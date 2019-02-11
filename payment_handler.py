"""Quickpay payment handler

DEPENDENCIES:
    quickpay-python-client
        pip install quickpay-api-client
        See https://github.com/QuickPay/quickpay-python-client
        Alternative installation: download and run "python3 setup.py install"

SETTINGS:
    QUICKPAY_API_KEY  = API key for access to QuickPay.
    QUICKPAY_ACQUIRER = Name of acquirer, defaults to 'nets'. Must be a valid acquirer.
    QUICKPAY_TESTMODE = Whether to run in testmode (= let test card payments through, real cards _ALWAYS_ go
                        through if there is a real acquirer active in QuickPay)

    To enable module, set
    SHOP_HANDLER_PAYMENT = 'quickpay.payment_handler.quickpay_payment_handler'

    SHOP_ORDER_PAID = order status to set when payment completed

TEST CARD:
    number = 1000020000000006
    expiration = 1609
    cvd = 123

    see list of test cards here: https://learn.quickpay.net/tech-talk/appendixes/test/
    cvd = 208 for test card issued in DK

TODO:
    Prevent double payment. Seen during test, probably due to double-click!
"""

from django.utils.timezone import now
from django.forms import Form
from django.utils.translation import ugettext_lazy as _
from django.core.urlresolvers import reverse
from django.dispatch import Signal
from django.http import HttpRequest
from django.db import transaction
from mezzanine.conf import settings
from cartridge.shop.models import Order
from cartridge.shop.checkout import CheckoutError, send_order_email
from .models import QuickpayPayment, quickpay_client
from quickpay_api_client.exceptions import ApiError
# noinspection PyPep8
import hmac, hashlib, locale, logging
from typing import Dict, Optional


__author__ = 'jfk@metation.dk'


# noinspection PyUnusedLocal
def quickpay_payment_handler(request, order_form: Form, order: Order) -> str:
    """Payment handler for credit card payments with own form in shop.

    Returns Quickpay transaction id -> written to Order.transaction_id in Cartridge checkout.

    To use Quickpay's payment window (mandatory for Mobilepay), see views.py. When using the payment window,
    this payment handler is unused.

    # TODO: test with QUICKPAY_ACQUIRER == None
    """
    assert isinstance(order, Order)
    # Get card data
    ofd = order_form.cleaned_data
    card_number = ofd['card_number']

    card_last4 = ofd['card_number'][-4:]
    # Expiry year is 4 digits (e.g. 2016) in Cartridge but 2 digits in Quickpay (e.g. 16)
    card_expiry = "%s%s" % ((ofd.get('card_expiry_year') or '')[2:], ofd.get('card_expiry_month') or '')
    card_ccv = ofd['card_ccv']
    logging.debug("quickpay_payment_handler(): card_number = XXXX XXXX XXXX %s, card_expiry = %s, card_ccv = XXXX"
                  % (card_last4, card_expiry))

    # Currency - the shop's currency. If we support multiple currencies in the future,
    # fetch currency from order instead.
    locale.setlocale(locale.LC_ALL, str(settings.SHOP_CURRENCY_LOCALE))
    currency = locale.localeconv()['int_curr_symbol'][0:3]
    logging.debug("quickpay_payment_handler(): currency = %s" % currency)

    payment = QuickpayPayment.create_card_payment(order, order.total, currency, card_last4)

    # Create payment
    client = quickpay_client()
    res = client.post('/payments', currency=currency, order_id='%s_%06d' % (order.id, payment.id))
    payment_id = res['id']
    logging.debug("quickpay_payment_handler(): Created payment with id=%s" % payment_id)

    # Authorize with credit card
    card = {'number': card_number, 'expiration': card_expiry, 'cvd': card_ccv}
    # noinspection PyPep8
    try:
        res = (client.post('/payments/%s/authorize?synchronized' % payment_id,
                           **{'amount': payment.requested_amount, 'card': card,
                              'acquirer': getattr(settings, 'QUICKPAY_ACQUIRER', None),
                              'auto_capture': settings.QUICKPAY_AUTO_CAPTURE}))
    except ApiError as e:
        logging.error("QuickPay API error: %s" % e.body)
        raise CheckoutError(_("Payment information invalid"))

    logging.debug("quickpay_payment_handler(): authorize result = %s" % res)
    print("QP result", res)
    payment.update_from_res(res)
    if payment.accepted or payment.test_mode:
        payment.accepted_date = now()
    payment.save()

    order.status = settings.SHOP_ORDER_PAID
    order.save()


    # Let payment through if in test mode no matter what
    if not settings.QUICKPAY_TESTMODE:
        if res['test_mode']:
            raise CheckoutError('Test card - cannot complete payment!')
        elif not res['accepted']:
            raise CheckoutError('Payment rejected by QuickPay or acquirer: "%s"'
                                % (res.get('qp_status_msg') or '(no message)'))

    return res['id']


def get_quickpay_link(order: Order) -> Dict[str, str]:
    """Get Quickpay link (as defined in Quickpay API) to pay a given Order.

    If both settings.QUICKPAY_ACQUIRER and settings.QUICKPAY_PAYMENT_METHODS are None or unspecified,
    the payment window will let the user choose any available payment method.
    """
    framed: bool = getattr(settings, 'QUICKPAY_FRAMED_MODE', False)
    currency = locale.localeconv()['int_curr_symbol'][0:3]
    card_last4 = '9999'
    payment = QuickpayPayment.create_card_payment(order, order.total, currency, card_last4)

    client = quickpay_client()
    res = client.post('/payments', currency=currency, order_id='%s_%06d' % (order.id, payment.id))
    payment_id = res['id']

    # Make continue_url, cancel_url for framed/unframed versions
    if framed:
        continue_url = reverse("quickpay_success_framed") + "?hash=" + sign_order(order)
        cancel_url = reverse("quickpay_failed_framed")
    else:
        continue_url = reverse("quickpay_success") + "?id="+str(order.pk) + "&hash=" + sign_order(order)
        cancel_url = reverse("quickpay_failed")

    # Make Quickpay link
    quickpay_link_args = dict(
        amount=payment.requested_amount,
        continue_url=settings.SHOP_URL + continue_url,
        cancel_url=settings.SHOP_URL + cancel_url,
        callback_url=settings.SHOP_URL + reverse('quickpay_callback'),
        auto_capture=getattr(settings, 'QUICKPAY_AUTO_CAPTURE', False),
        language=getattr(settings, 'QUICKPAY_LANGUAGE', 'da'),
        framed=framed,
    )
    if getattr(settings, 'QUICKPAY_ACQUIRER', None):
        quickpay_link_args['acquirer'] = settings.QUICKPAY_ACQUIRER
    if getattr(settings, 'QUICKPAY_PAYMENT_METHODS', None):
        quickpay_link_args['payment_methods'] = settings.QUICKPAY_PAYMENT_METHODS

    return client.put("/payments/%s/link" % payment_id, **quickpay_link_args)


def sign(base: bytes, private_key: str) -> str:
    """Calculate callback signature"""
    return hmac.new(
      bytes(private_key, "utf-8"),
      base,
      hashlib.sha256
    ).hexdigest()


def sign_order(order: Order) -> str:
    """Calculate order order signature"""
    return sign(bytes(str(order.pk) + str(order.total) + order.key, 'utf-8'), settings.QUICKPAY_PRIVATE_KEY)


# Signal when order has been authorized. Sent once per order.
# Called within a transaction
order_authorized = Signal(providing_args=['instance'])


# Signal when order has been completed. Sent once per order. NOT SENT if success page not reached!
# Called within a transaction
order_completed = Signal(providing_args=['instance'])


def order_handler(request: Optional[HttpRequest], order_form, order: Order):
    """Order paid in Quickpay payment window. Do not use for Quickpay API mode.

    request and order_form unused.

    Safe to call multiple times for same order (IS CALLED in payment process and in payment handler callback)

    NB: order.complete() is done here! With standard Cartridge credit card flow, order.complete() is called there!
    This is because we want complete() to be called within the atomic transaction!
    """

    completed_now = False
    with transaction.atomic():
        transaction_id = order.transaction_id
        # Re-read the order from the database to make sure it locked for atomicity
        order: Order = Order.objects.filter(pk=order.pk).select_for_update()[0]
        status_authorized = getattr(settings, 'QUICKPAY_ORDER_STATUS_AUTHORIZED', None)
        if status_authorized and order.status < status_authorized or not order.transaction_id:
            print("order_handler() - called", order)
            if status_authorized:
                order.status = status_authorized

            if transaction_id:
                print("order_handler() - save transaction_id {}".format(transaction_id))
                order.transaction_id = transaction_id
            order.save()

            order_authorized.send(sender=Order, instance=order)
        else:
            print("order_handler() - order {} already being processed".format(order.id))

        # Complete Order (delete basket, etc.)
        # Possible problem: stock and discount usages not counted down if success URL not reached
        if request is not None:
            print("order_handler() - calling order.complete()")
            status_waiting = getattr(settings, 'QUICKPAY_ORDER_STATUS_WAITING', None)
            if status_waiting and order.status < status_waiting:
                completed_now = True
                order.status = status_waiting
                order.complete(request)  # Saves, deletes basket
                order_completed.send(sender=Order, instance=order)

    if request is not None and completed_now:
        # Send mail to customer on success
        # Mail isn't sent if success page isn't reached. Shop admin can see that - the order will be in
        # ORDER_STATUS_AUTHORIZED whereas if the success page was reached, it's in _WAITING.
        # Outside transaction to shorten transaction time and to prevent transaction rollback if mail fails
        send_order_email(request, order)
