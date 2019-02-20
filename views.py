from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse, \
    HttpResponseBadRequest, HttpResponseForbidden
from django.template import loader
from django.template.response import TemplateResponse
from django.shortcuts import redirect, render
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt
from django.core.urlresolvers import reverse
from django.db import transaction

from mezzanine.conf import settings
from mezzanine.utils.importing import import_dotted_path
from cartridge.shop import checkout
from cartridge.shop.models import Order
from cartridge.shop.forms import OrderForm

import json, logging
from urllib.parse import urlencode
from typing import List

from .payment import get_quickpay_link, sign, sign_order
from .models import QuickpayPayment


handler = lambda s: import_dotted_path(s) if s else lambda *args: None
billship_handler = handler(settings.SHOP_HANDLER_BILLING_SHIPPING)
tax_handler = handler(settings.SHOP_HANDLER_TAX)
order_handler = handler(settings.SHOP_HANDLER_ORDER)
order_form_class = (lambda s: import_dotted_path(s) if s else OrderForm)(getattr(settings, 'QUICKPAY_ORDER_FORM', None))


def quickpay_checkout(request: HttpRequest) -> HttpResponse:
    """Checkout using Quickpay payment form.

    Use the normal cartridge.views.checkout_steps for GET and for the rest other payments steps,
    use this special version for POSTing paument form for Quickpay.

    Settings:

    QUICKPAY_ORDER_FORM = dotted path to order form to use
    QUICKPAY_FRAMED_MODE = <whether to use framed Quickpay>

    QUICKPAY_SHOP_BASE_URL: str required = URL of the shop for success, cancel and callback URLs
    QUICKPAY_ACQUIRER: str required = The acquirer to use, e.g. 'clearhaus'
    QUICKPAY_AUTO_CAPTURE: bool default False = Whether to auto-capture payment

    urls.py setup:

    from cartridge_quickpay.views import checkout_quickpay, order_form_class
    ...

    url("^shop/checkout/", checkout_steps, {'form_class': order_form_class}),
    url("^shop/checkout_quickpay/", checkout_quickpay, name="checkout_quickpay"),
    url("^shop/", include("cartridge.shop.urls")),
    ...

    ** FOR FRAMED MODE: **

    Change checkout.html
    - <form ... onsubmit="return false">
    - Change submit button to:
    - <button class="btn btn-lg btn-primary pull-right" onclick="checkout_quickpay();">Go to payment</button>
    - add payment modal

    <div class="modal db-modal fade" id="payment_window" tabindex="-1" role="dialog" aria-labelledby="payment_window_label">
      <div class="modal-dialog" role="document">
        <div class="modal-content">
          <div class="modal-body">
              <iframe id="payment_iframe" style="width: 100%; border: none; height: 90vh;"></iframe>
          </div>
        </div>
      </div>
    </div>

    - and add JS at the bottom:

    <script>
    function checkout_quickpay() {
      $.post("{% url 'quickpay_checkout' %}", $('.checkout-form').serialize(), function(data) {
        if (data.success) {
          $('#payment_iframe').attr('src', data.payment_link);
          $('#payment_window').modal('show');
        } else {
          alert("failed");
        }
      });
    }
    </script>
    """
    framed: bool = getattr(settings, 'QUICKPAY_FRAMED_MODE', False)
    step = checkout.CHECKOUT_STEP_LAST
    checkout_errors = []

    initial = checkout.initial_order_data(request, order_form_class)
    form = order_form_class(request, step, initial=initial, data=request.POST)
    if form.is_valid():
        print("** Form valid")
        request.session["order"] = dict(form.cleaned_data)
        try:
            billship_handler(request, form)
            tax_handler(request, form)
        except checkout.CheckoutError as e:
            print("** billship or tax handler failed")
            checkout_errors.append(e)

        # Create order and Quickpay payment, redirect to Quickpay/Mobilepay form
        order = form.save(commit=False)
        order.setup(request)  # Order is saved here so it gets an ID

        quickpay_link: str = get_quickpay_link(order)['url']

        # Redirect to Quickpay
        if framed:
            print("** JSON response", {'success': True, 'payment_link': quickpay_link})
            return JsonResponse({'success': True, 'payment_link': quickpay_link})
        else:
            print("** Redirect response")
            return HttpResponseRedirect(redirect_to=quickpay_link)

    # Form invalid, go back to checkout step
    step_vars = checkout.CHECKOUT_STEPS[step - 1]
    template = "shop/%s.html" % step_vars["template"]
    context = {"CHECKOUT_STEP_FIRST": step == checkout.CHECKOUT_STEP_FIRST,
               "CHECKOUT_STEP_LAST": step == checkout.CHECKOUT_STEP_LAST,
               "CHECKOUT_STEP_PAYMENT": (settings.SHOP_PAYMENT_STEP_ENABLED and
                   step == checkout.CHECKOUT_STEP_PAYMENT),
               "step_title": step_vars["title"], "step_url": step_vars["url"],
               "steps": checkout.CHECKOUT_STEPS, "step": step, "form": form,
               "payment_url": "https://payment.quickpay.net/d7ad25ea15154ef4bdffb5bf78f623fc"}

    page = loader.get_template(template).render(context=context, request=request)
    if framed:
        print("** form not ok, JSON")
        return JsonResponse({'success': False, 'page': page})
    else:
        print("** form not ok, page")
        return HttpResponse(page)


def failed(request: HttpRequest):
    """Payment failed"""
    qp_failed_url = getattr(settings, 'QUICKPAY_FAILED_URL', '')
    if qp_failed_url:
        return HttpResponseRedirect(qp_failed_url)
    else:
        # Assumes the template is available...
        return render(request, "shop/quickpay_failed.html")


def failed_framed(request: HttpRequest) -> HttpResponse:
    """Failed in an iframe, redirect the root page"""
    res = """
<html>
<head>
<script>
window.parent.location.replace("{}")
</script>
</head>
</html>
""".format(reverse('quickpay_failed'))
    return HttpResponse(res)


def success(request: HttpRequest) -> HttpResponse:
    """Quickpay payment succeeded.

    GET args:
      id : int = ID of order
      hash : str = signature hash of order. Raise

    NB: Form not available (quickpay order handler)
    NB: Only safe to call more than once if order_handler is
    """
    order_id = request.GET.get('id')
    if order_id:
        order = Order.objects.get(pk=order_id)
    else:
        order = Order.objects.from_request(request)  # Raises DoesNotExist if order not found
    print("** SUCCESS order =", order, "sign arg =", request.GET.get('hash'), "check sign =", sign_order(order))

    # Check hash.
    if request.GET.get('hash') != sign_order(order):
        return HttpResponseForbidden()

    # Call order handler
    order_handler(request, order_form=None, order=order)

    response = redirect("shop_complete")
    return response


def success_framed(request: HttpRequest) -> HttpResponse:
    """Succeeded in and iframe, redirect the root page"""
    params = urlencode(request.GET)
    print("success_iframe PARAMS", params)
    res = """
<html>
<head>
<script>
window.parent.location.replace("{}?{}")
</script>
</head>
</html>
""".format(reverse('quickpay_success'), params)
    return HttpResponse(res)


@csrf_exempt
def callback(request: HttpRequest) -> HttpResponse:
    """Callback from Quickpay. Register payment status in case it wasn't registered already"""
    # callback() itself only updates
    data = json.loads(request.body.decode('utf-8'))
    if settings.DEBUG:
        print("Callback() from Quickpay")
        print(data)

    # Check checksum
    checksum = sign(request.body, settings.QUICKPAY_PRIVATE_KEY)
    if settings.DEBUG:
        print("Private key =", settings.QUICKPAY_PRIVATE_KEY)
        print("Request checksum =", request.META['HTTP_QUICKPAY_CHECKSUM_SHA256'])
        print("Calculated checksum =", checksum)
    if checksum != request.META['HTTP_QUICKPAY_CHECKSUM_SHA256']:
        logging.error('Quickpay callback: checksum failed {}'.format(data))
        return HttpResponseBadRequest()

    # when auto capture is on, we get two callbacks:
    # 1) authorization callback with data['state'] == 'new'
    # 2) capture callback with data['state'] == 'processed'
    # if auto capture is on, ignore the first callback
    if settings.QUICKPAY_AUTO_CAPTURE and data.get('state', None) != 'processed':
        return HttpResponse("OK")


    # in checkout_mobilepay we did this: order_id='%s_%06d' % (order.id, payment.id)
    order_id_payment_id_string = data.get('order_id','')
    print('order_id_payment_id_string: {}'.format(order_id_payment_id_string))
    separator_index = order_id_payment_id_string.find('_')
    if separator_index > -1:
        order_id = order_id_payment_id_string[:separator_index]
    else:
        order_id = ''
    print('order_id: {}'.format(order_id))
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        # Order not found, ignore
        return HttpResponse("OK")

    print("data['accepted']: {}".format(data['accepted']))
    print("data['test_mode']: {}".format(data['test_mode']))
    print("order.status: {}".format(order.status))
    if data['accepted']:
        with transaction.atomic():
            qpps: List[QuickpayPayment] = list(
                QuickpayPayment.objects.filter(order=order).order_by('-id').select_for_update())
            if qpps:
                qpp = qpps[0]
                qpp.update_from_res(data)
                if qpp.accepted or qpp.test_mode:
                    qpp.accepted_date = now()
                    if settings.QUICKPAY_AUTO_CAPTURE:
                        qpp.captured_date = qpp.accepted_date
                qpp.save()
            order.transaction_id = data['id']
            order_handler(request=None, order_form=None, order=order)

    print("Callback() - final order.status: {}".format(order.status))

    return HttpResponse("OK")
