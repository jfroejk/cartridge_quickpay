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

import json
import logging
import re
from urllib.parse import urlencode
from typing import Callable, List, Optional

from .payment import get_quickpay_link, sign, sign_order, start_subscription, capture_subscription_order, \
     acquirer_requires_popup, acquirer_supports_subscriptions, order_currency
from .models import QuickpayPayment, get_private_key


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
    QUICKPAY_ACQUIRER: str|list required = The acquirer(s) to use, e.g. 'clearhaus'
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
    acquirer = request.POST.get('acquirer', None)
    logging.debug("quickpay_checkout: using acquirer {}".format(acquirer or '<any>'))
    in_popup = acquirer_requires_popup(acquirer)
    step = checkout.CHECKOUT_STEP_FIRST  # Was: _LAST
    checkout_errors = []

    initial = checkout.initial_order_data(request, order_form_class)
    logging.debug("quickpay_checkout: initial order data = {}".format(initial))
    form = order_form_class(request, step, initial=initial, data=request.POST)
    if form.is_valid():
        logging.debug("quickpay_checkout() - Form valid")
        request.session["order"] = dict(form.cleaned_data)
        try:
            billship_handler(request, form)
            tax_handler(request, form)
        except checkout.CheckoutError as e:
            logging.warn("quickpay_checkout() - billship or tax handler failed")
            checkout_errors.append(e)

        # Create order and Quickpay payment, redirect to Quickpay/Mobilepay form
        order = form.save(commit=False)
        order.setup(request)  # Order is saved here so it gets an ID

        # Handle subscription or one-time order
        if (hasattr(order, 'has_subscription')
                and order.has_subscription()
                and acquirer_supports_subscriptions(acquirer)):
            quickpay_subs_id, quickpay_link = start_subscription(
                order, order.items.all().order_by('id')[0])
            logging.debug("quickpay_checkout() - starting subscription {}, payment link {}"
                          .format(quickpay_subs_id, quickpay_link))
        else:
            # One-time order OR subscription with acquirer that doesn't support subscriptions
            quickpay_link: str = get_quickpay_link(order, acquirer)['url']
            logging.debug("quickpay_checkout() - product purchase (or subscription w/o auto-renewal), payment link {}"
                          .format(quickpay_link))

        # Redirect to Quickpay
        if framed:
            logging.debug("quickpay_checkout() - JSON response {}"
                          .format(str({'success': True, 'payment_link': quickpay_link})))
            return JsonResponse({'success': True, 'payment_link': quickpay_link})
            # Medsende om url skal åbnes i nyt vindue, åben i JS, håndtere at returside havner i iframe igen
        elif in_popup:
            logging.debug("quickpay_checkout() - Opening popup window")
            return render(request, "cartridge_quickpay/payment_toplevel.html", {'quickpay_link': quickpay_link})
        else:
            logging.debug("quickpay_checkout() - Redirect response")
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
        logging.debug("quickpay_checkout() - Form not OK, JSON response")
        return JsonResponse({'success': False, 'page': page})
    else:
        logging.debug("quickpay_checkout() - Form not OK, page response")
        return HttpResponse(page)


def escape_frame(f: Callable[[HttpRequest], HttpResponse]) -> Callable[[HttpRequest], HttpResponse]:
    """Escape iframe when payment is in a iframe and the shop itself is not"""
    def f_escape(request: HttpRequest) -> HttpResponse:
        if request.GET.get('framed'):
            logging.debug("cartridge_quickpay.views.escape_frame: Escaping")
            url = request.path
            get_args = request.GET.copy()
            get_args.pop('framed')
            if get_args:
                url += '?' + get_args.urlencode()
            res = '<html><head><script>window.parent.location.replace("{}");</script></head></html>'.format(url)
            return HttpResponse(res)
        else:
            logging.debug("cartridge_quickpay.views.escape_frame: NOT in frame")
            return f(request)

    f_escape.__name__ = f.__name__
    return f_escape


def escape_popup(f: Callable[[HttpRequest], HttpResponse]) -> Callable[[HttpRequest], HttpResponse]:
    """Escape payment popup window"""
    def f_escape(request: HttpRequest) -> HttpResponse:
        if request.GET.get('popup'):
            logging.debug("cartridge_quickpay.views.escape_popup: Escaping")
            url = request.path
            get_args = request.GET.copy()
            get_args.pop('popup')
            if get_args:
                url += '?' + get_args.urlencode()
            res = '<html><head><script>var opener = window.opener; opener.document.location = "{}"; window.close(); opener.focus();</script></head></html>'.format(url)
            return HttpResponse(res)
        else:
            logging.debug("cartridge_quickpay.views.escape_popup: NOT in popup")
            return f(request)

    f_escape.__name__ = f.__name__
    return f_escape


@escape_frame
@escape_popup
def failed(request: HttpRequest):
    """Payment failed"""
    logging.warning("payment_quickpay.views.failed(), GET args = {}".format(request.GET))
    qp_failed_url = getattr(settings, 'QUICKPAY_FAILED_URL', '')
    if qp_failed_url:
        return HttpResponseRedirect(qp_failed_url)
    else:
        # Assumes the template is available...
        return render(request, "shop/quickpay_failed.html")


@escape_frame
@escape_popup
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
    order_hash = sign_order(order)
    logging.debug("\n ---- payment_quickpay.views.success()\n\norder = %s, sign arg = %s, check sign = %s"
                  % (order, request.GET.get('hash'), sign_order(order)))
    logging.debug("data: {}".format(dict(request.GET)))

    # Check hash.
    if request.GET.get('hash') != order_hash:
        logging.warn("cartridge_quickpay:success - hash doesn't match order")
        return HttpResponseForbidden()

    # Call order handler
    order_handler(request, order_form=None, order=order)

    response = redirect("shop_complete")
    return response


try:
    from cartridge_subscription.models import Subscription, SubscriptionPeriod
except ImportError:
    Subscription = None


@csrf_exempt
@transaction.atomic
def callback(request: HttpRequest) -> HttpResponse:
    """Callback from Quickpay. Register payment status in case it wasn't registered already"""

    def update_payment() -> Optional[QuickpayPayment]:
        """Update QuickPay payment from Quickpay result"""
        # Refers order, data from outer scope
        payment: Optional[QuickpayPayment] = QuickpayPayment.get_order_payment(order)
        if payment is not None:
            payment.update_from_res(data)  # NB: qp.test_mode == data['test_mode']
            payment.save()
        return payment
        
    data = json.loads(request.body.decode('utf-8'))
    logging.debug("\n ---- payment_quickpay.views.callback() ----")
    logging.debug("Got data {}\n".format(data))

    # We may get several callbacks with states "new", "pending", or "processed"
    # We're only interested in "processed" for payments and "active" for new subscriptions
    qp_state = data.get('state', None)
    if (qp_state in ('processed', 'active', 'rejected')
            or not getattr(settings, 'QUICKPAY_AUTO_CAPTURE', False) and qp_state == 'pending'):
        logging.debug("payment_quickpay.views.callback(): QP state is {}, processing".format(qp_state))
    else:
        logging.debug("payment_quickpay.views.callback(): QP state is {}, skipping".format(qp_state))
        return HttpResponse("OK")

    # Get the order
    order_id_payment_id_string = data.get('order_id','')
    logging.debug('order_id_payment_id_string: {}'.format(order_id_payment_id_string))
    order_id = re.sub('_\d+', '', order_id_payment_id_string)
    logging.debug('order_id: {}'.format(order_id))
    try:
        order = Order.objects.filter(pk=order_id).select_for_update()[0]  # Lock order to prevent race condition
    except IndexError:
        # Order not found, ignore
        logging.warning("payment_quickpay.views.callback(): order id {} not found, skipping".format(order_id))
        return HttpResponse("OK")

    # Check checksum. If we have multiple agreements, we need the order currency to get the right one
    checksum = sign(request.body, get_private_key(order_currency(order)))
    logging.debug("Request checksum = {}".format(request.META['HTTP_QUICKPAY_CHECKSUM_SHA256']))
    logging.debug("Calculated checksum = {}".format(checksum))
    if checksum != request.META['HTTP_QUICKPAY_CHECKSUM_SHA256']:
        logging.error('Quickpay callback: checksum failed {}'.format(data))
        return HttpResponseBadRequest()

    logging.debug("payment_quickpay.views.callback(): order.status = {}".format(order.status))

    if data['state'] == 'rejected':
        update_payment()

    elif data['type'] == 'Subscription' and Subscription is not None:
        # Starting a NEW subscription. The Subscription is created in order_handler
        logging.error("payment_quickpay.views.callback(): starting subscription, order {}".format(order.id))

        # Capture the initial subscription payment
        capture_subscription_order(order)  # Starts async capture, next callback is 'accepted'

    elif data['accepted']:
        # Normal or subscription payment
        # If autocapture, the payment will have been captured.
        # If not autocapture, the payment will have been reserved only and must be captured later.

        # -- The order can be considered paid (reserved or captured) if and only if we get here.
        # -- An order is paid if and only if it has a transaction_id
        logging.info("payment_quickpay.views.callback(): accepted payment, order {}".format(order.id))
        payment = update_payment()
        order.transaction_id = data['id']
        logging.debug("payment_quickpay.views.callback(): calling order_handler, qp subscription = {}"
                      .format(data.get('subscription_id', '-')))
        order_handler(request=None, order_form=None, order=order, payment=payment)

    logging.debug("payment_quickpay.views.callback(): final order.status: {}".format(order.status))

    return HttpResponse("OK")
