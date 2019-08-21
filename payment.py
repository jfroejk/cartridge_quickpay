"""Quickpay payment handler

DEPENDENCIES:
    quickpay-python-client
        pip install quickpay-api-client
        See https://github.com/QuickPay/quickpay-python-client
        Alternative installation: download and run "python3 setup.py install"

SETTINGS:
    QUICKPAY_API_KEY  = API key for access to QuickPay.
    QUICKPAY_ACQUIRER = Name of acquirer, defaults to 'nets'. Must be a valid acquirer.
                        Set explicitly to None for "any acquirer"
                        Nets without API key can be used for test
    QUICKPAY_TESTMODE = Whether to run in testmode (= let test card payments through, real cards _ALWAYS_ go
                        through if there is a real acquirer active in QuickPay)

    To enable module, set
    SHOP_HANDLER_PAYMENT = 'quickpay.payment_handler.quickpay_payment_handler'

    SHOP_ORDER_PAID = order status to set when payment completed

TEST CARD (Nets, Dankort):
    number = 1000020000000006
    expiration = 12 99
    cvd = 123

    Clearhaus, VISA:
    number: 1000 0000 0000 0008
    expiration = 12 99
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
from django.dispatch import Signal, receiver
from django.http import HttpRequest
from django.db import transaction
from mezzanine.conf import settings
from cartridge.shop.models import Order, OrderItem
from cartridge.shop.checkout import CheckoutError, send_order_email
from .models import QuickpayPayment, quickpay_client, get_private_key
from quickpay_api_client.exceptions import ApiError
# noinspection PyPep8
import hmac, hashlib, locale, logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

try:
    from cartridge_subscription.models import Subscription, SubscriptionPeriod
except ImportError:
    Subscription = None


__author__ = 'jfk@metation.dk'


_ACQUIRES_REQUIRING_POPUP = ['paypal', 'applepay']         # Add others requires that require a separate browser window
_ACQUIRES_SUPPORTING_SUBSCRIPTION = ['nets', 'clearhaus']  # Add others as applicable.
                                                           # Ensure recurring payments enabled with acquirer

def acquirer_requires_popup(acquirer: Optional[str]) -> bool:
    """Whether acquirer requires a popup windows (iframe not allowed)"""
    return acquirer in _ACQUIRES_REQUIRING_POPUP


def acquirer_supports_subscriptions(acquirer: Optional[str]) -> bool:
    """Whether acquirer supports subscriptions. Return True if acquirer is None == use any acquirer,
    assume at least one of them has subscriptions enabled"""
    return acquirer in _ACQUIRES_SUPPORTING_SUBSCRIPTION if acquirer else True


def enabled_acquirers() -> List[str]:
    """Return enabled acquirers, empty list if no one given explicitly"""
    acquirers = getattr(settings, 'QUICKPAY_ACQUIRERS', [])
    return [acquirers] if type(acquirers) is str else acquirers


# noinspection PyUnusedLocal
def quickpay_payment_handler(request, order_form: Form, order: Order) -> str:
    """Payment handler for credit card payments with own form in shop.

    Returns Quickpay transaction id -> written to Order.transaction_id in Cartridge checkout.

    To use Quickpay's payment window (mandatory for Mobilepay), see views.py. When using the payment window,
    this payment handler is unused.

    # TODO: test with QUICKPAY_ACQUIRER == None
    # TODO: make it work without 'syncronized' - deprecated by Quickpay
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
    currency = order_currency(order)
    logging.debug("quickpay_payment_handler(): currency = %s" % currency)

    payment = QuickpayPayment.create_card_payment(order, order.total, currency, card_last4)

    # Create payment
    client = quickpay_client(currency)
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
                              'auto_capture': getattr(settings, 'QUICKPAY_AUTO_CAPTURE', False)}))
    except ApiError as e:
        logging.error("QuickPay API error: %s" % e.body)
        raise CheckoutError(_("Payment information invalid"))

    logging.debug("quickpay_payment_handler(): authorize result = %s" % res)
    payment.update_from_res(res)
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


def get_quickpay_link(order: Order, acquirer: Optional[str] = None) -> Dict[str, str]:
    """Get Quickpay link (as defined in Quickpay API) to pay a given Order.

    If both settings.QUICKPAY_ACQUIRER and settings.QUICKPAY_PAYMENT_METHODS are None or unspecified,
    the payment window will let the user choose any available payment method.
    """
    logging.debug("payment_quickpay: get_quickpay_link() - link for {}".format(order))
    framed: bool = getattr(settings, 'QUICKPAY_FRAMED_MODE', False)
    iframe: bool = getattr(settings, 'QUICKPAY_IFRAME_MODE', False)
    currency = order_currency(order)
    card_last4 = '9999'
    with transaction.atomic():
        payment = QuickpayPayment.create_card_payment(order, order.total, currency, card_last4)

        client = quickpay_client(currency)
        qp_order_id = '%s_%06d' % (order.id, payment.id)
        res = client.post('/payments', currency=currency, order_id=qp_order_id)
        payment_id = res['id']
        payment.qp_id = payment_id
        payment.save()
    logging.debug(
        "payment_quickpay: get_quickpay_link() - created Quickpay payment with order_id={}, payment id={}"
        .format(qp_order_id, res['id']))

    # Make continue_url, cancel_url for framed/unframed versions
    cancel_url = reverse("quickpay_failed")
    continue_url = reverse("quickpay_success") + "?id="+str(order.pk) + "&hash=" + sign_order(order)
    if framed:
        continue_url += '&framed=1'
        cancel_url += '?framed=1'
    elif acquirer_requires_popup(acquirer):
        framed = iframe = False  # Make sure 'framed' parameter to Quickpay is False when paying in a popup window
        continue_url += '&popup=1'
        cancel_url += '?popup=1'

    # Make Quickpay link
    quickpay_link_args = dict(
        amount=payment.requested_amount,
        continue_url=settings.QUICKPAY_SHOP_BASE_URL + continue_url,
        cancel_url=settings.QUICKPAY_SHOP_BASE_URL + cancel_url,
        callback_url=settings.QUICKPAY_SHOP_BASE_URL + reverse('quickpay_callback'),
        auto_capture=getattr(settings, 'QUICKPAY_AUTO_CAPTURE', False),
        language=getattr(settings, 'QUICKPAY_LANGUAGE', 'en'),
        framed=framed or iframe,
        customer_email = order.billing_detail_email,
    )
    if acquirer:
        quickpay_link_args['acquirer'] = acquirer
        logging.debug("payment_quickpay: get_quickpay_link() - acquirer = '{}' (from arg)"
                      .format(acquirer))
    elif getattr(settings, 'QUICKPAY_ACQUIRER', None):
        quickpay_link_args['acquirer'] = settings.QUICKPAY_ACQUIRER
        logging.debug("payment_quickpay: get_quickpay_link() - acquirer = '{}' (from settings)"
                      .format(settings.QUICKPAY_ACQUIRER))
    if getattr(settings, 'QUICKPAY_PAYMENT_METHODS', None):
        quickpay_link_args['payment_methods'] = settings.QUICKPAY_PAYMENT_METHODS
        logging.debug("payment_quickpay: get_quickpay_link() - payement methods = '{}'"
                      .format(settings.QUICKPAY_PAYMENT_METHODS))

    logging.debug(
        "payment_quickpay: get_quickpay_link() - creating link with args {}".format(str(quickpay_link_args)))
    res = client.put("/payments/%s/link" % payment_id, **quickpay_link_args)
    logging.debug(
        "payment_quickpay: get_quickpay_link() - got link {}".format(res))
    return res


def delete_payment_link(payment: QuickpayPayment):
    """Delete payment link in Quickpay.
    Requires permission for the API user in Quickpay (Settings > Users > API User > /payments/:id/link delete
    """
    if payment.qp_id and not (payment.accepted_date or payment.captured_date):
        client = quickpay_client(payment.requested_currency)
        url = "/payments/{}/link".format(payment.qp_id)
        logging.debug("cartridge_quickpay.payment.delete_payment_link: delete({})".format(url))
        # print(client.delete(url))
        # Ignore if the payment can't be cancelled
        try:
            url = "/payments/{}/cancel".format(payment.qp_id)
        except:
            pass
        # print(client.post(url))


@transaction.atomic
def start_subscription(order: Order, order_item: OrderItem) -> Tuple[int, str]:
    """Start subscription and get subscription authorization link.
    Returns (<Quickpay subscription id>, <Quickpay payment url>)

    Procedure:
      Create the subscription order in the shop
      Call start_subscription() to create the QP subscription. The subscription id is registered as order.membership_id
      Redirect the user to the returned payment URL
      The user makes a normal payment. 
    """
    currency = order_currency(order)
    amount: Decimal = order_item.total_price + getattr(order_item, 'tax_amount', 0)
    payment = QuickpayPayment.create_card_payment(order, amount, currency, '9999')

    # Create subscription in Quickpay
    client = quickpay_client(currency)
    qp_order_id = "%04d" % order.id  # Quickpay requires 4..20 chars in order ID
    res = client.post("/subscriptions", order_id=qp_order_id, currency=currency, description=order_item.description)
    logging.debug("start_subscription qp /subscriptions POST result = {}".format(res))
    subscription_id = res['id']

    # Make continue_url, cancel_url for framed/unframed versions
    framed: bool = getattr(settings, 'QUICKPAY_FRAMED_MODE', False)
    iframe: bool = getattr(settings, 'QUICKPAY_IFRAME_MODE', False)
    continue_url = reverse("quickpay_success") + "?id="+str(order.pk) + "&hash=" + sign_order(order)
    cancel_url = reverse("quickpay_failed")
    if framed:
        continue_url += '&framed=1'
        cancel_url += '?framed=1'

    # Make Quickpay link
    int_amount = int(amount * 100)
    quickpay_link_args = dict(
        amount=int_amount,
        continue_url=settings.QUICKPAY_SHOP_BASE_URL + continue_url,
        cancel_url=settings.QUICKPAY_SHOP_BASE_URL + cancel_url,
        callback_url=settings.QUICKPAY_SHOP_BASE_URL + reverse('quickpay_callback'),
        language=getattr(settings, 'QUICKPAY_LANGUAGE', 'en'),
        framed=framed or iframe,
    )
    quickpay_link_args['customer_email'] = order.billing_detail_email
    # quickpay_link_args['acquirer'] = 'paypal' - FOR TEST
    logging.debug("start_subscription qp /subscriptions/{}/link args: {}".format(subscription_id, quickpay_link_args))

    res = client.put('/subscriptions/{}/link'.format(subscription_id), **quickpay_link_args)
    logging.debug("start_subscription qp /subscriptions/{}/link result = {}".format(subscription_id, res))
    url = res['url']

    if Subscription is not None:
        order.membership_id = subscription_id
        order.save()

    return subscription_id, url
    

def renew_subscription(subscription: 'Subscription', product_sku: Optional[str] = None,
                       from_time: Optional[datetime] = None) -> Optional['Order']:
    """Make subscription renewal and capture renewal price. Assume order price is correct.
    """
    order: Optional['Order'] = subscription.renew(product_sku, from_time)
    if order is not None:
        # Capture and callback from Quickpay handled here, only internals handled before this point.
        capture_subscription_order(order)  # Async - finished up in callback
    return order


@transaction.atomic
def capture_subscription_order(order: Order):
    """Capture initial or recurring subscription order.

    Makes a QuickpayPayment instance with the QP payment id but does not modify any other data.
    The capture is finished in the callback.

    Before capturing:
    - Subscription payment must be authorized
    """
    currency = order_currency(order)
    client = quickpay_client(currency)
    amount = order.total
    Order.objects.filter(pk=order.pk).select_for_update()[0]  # Lock order to prevent race condition
    payment = (QuickpayPayment.get_order_payment(order)
                   or QuickpayPayment.create_card_payment(order, amount, currency, '9999'))
    qp_order_id = '%s_%06d' % (order.id, payment.id)
    int_amount = int(amount * 100)
    url = "/subscriptions/{}/recurring".format(order.membership_id)
    args = {'order_id': qp_order_id, 'amount': int_amount, 'auto_capture': True, 'synchronized': True}
    logging.debug("payment_quickpay:capture_subscription: recurring capture, url={}, args={}".format(url, args))
    res = client.post(url, **args)
    logging.debug("payment_quickpay:capture_subscription res = {}".format(res))
    payment.qp_id = res['id']
    payment.save()


def delete_order_subscription(order: Order):
    """Delete order subscription in Quickpay if it has never been paid/active
    Requires permission for the API user in Quickpay (Settings > Users > API User > /subscription/:id/link delete
    """
    if order.status == settings.ORDER_STATUS_NEW and not order.transaction_id and getattr(order, 'membership_id', None):
        client = quickpay_client(order.currency)
        url = "/subscriptions/{}/link".format(order.membership_id)
        logging.debug("cartridge_quickpay.payment.delete_order_subscription: delete({})".format(url))
        client.delete(url)
        url = "/subscriptions/{}/cancel".format(order.membership_id)
        # Ignore if subscription cannot be cancelled in qp
        try:
            client.post(url)
        except:
            pass


def order_currency(order: Order) -> str:
    return getattr(order, 'currency') or locale.localeconv()['int_curr_symbol'][0:3]


def sign(base: bytes, private_key: str) -> str:
    """Calculate callback signature"""
    return hmac.new(
      bytes(private_key, "utf-8"),
      base,
      hashlib.sha256
    ).hexdigest()


def sign_order(order: Order) -> str:
    """Calculate order order signature"""
    # order.total may have more decimals than are saved, round to make sure it has exactly two
    sign_string = str(order.pk) + str(round(order.total, 2)) + order.key
    logging.debug("cartridge_quickpay:sign_order() - sign string = '{}'".format(sign_string))
    res = sign(bytes(sign_string, 'utf-8'), get_private_key(order_currency(order)))
    logging.debug("cartridge_quickpay:sign_order() - signature = '{}'".format(res))
    return res


# Signal when order has been authorized. Sent once per order.
# Called within a transaction
order_authorized = Signal(providing_args=['instance', 'payment'])
order_captured = Signal(providing_args=['instance', 'payment'])


# Signal when order has been completed. Sent once per order. NOT SENT if success page not reached!
# Called within a transaction
order_completed = Signal(providing_args=['instance'])


# Signal when order has been completed. Sent once per order. NOT SENT if success page not reached!
# Called within a transaction
subscription_paid = Signal(providing_args=['instance'])


def order_handler(request: Optional[HttpRequest], order_form, order: Order, payment: Optional[QuickpayPayment] = None):
    """Order paid in Quickpay payment window. Do not use for Quickpay API mode.

    request and order_form unused.

    Safe to call multiple times for same order (IS CALLED in payment process and in payment handler callback)

    NB: order.complete() is done here! With standard Cartridge credit card flow, order.complete() is called there!
    This is because we want complete() to be called within the atomic transaction!
    """

    completed_now = False
    with transaction.atomic():
        transaction_id = order.transaction_id
        # Re-read the order from the database to make sure it locked for atomicity.
        # This is important when calling order_handler from success()
        order: Order = Order.objects.filter(pk=order.pk).select_for_update()[0]
        status_authorized = getattr(settings, 'QUICKPAY_ORDER_STATUS_AUTHORIZED', None)
        if status_authorized and order.status < status_authorized or not order.transaction_id:
            logging.debug("payment_quickpay: order_handler(), order = %s" % order)
            if status_authorized:
                order.status = status_authorized

            if transaction_id:
                logging.debug("order_handler() - save transaction_id {}".format(transaction_id))
                order.transaction_id = transaction_id

            order.save()
            if order.transaction_id:
                if payment and payment.is_captured:
                    order_captured.send(sender=Order, instance=order, payment=payment)
                else:
                    order_authorized.send(sender=Order, instance=order, payment=payment)
        else:
            logging.debug("order_handler() - order {} already being processed".format(order.id))

        # Complete Order (delete basket, etc.). Not guaranteed to happen, e.g if user closes the browser too early
        # Possible problem: stock and discount usages not counted down if success URL not reached
        if request is not None:
            logging.debug("order_handler() - calling order.complete()")
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


if Subscription is not None:
    @receiver(order_captured, sender=Order, dispatch_uid='register_subscription_order_captured')
    def subscribe_on_order_captured(sender, instance: Order, **kwargs):
        """Create Subscription when subscription Order is done.
        May be a Quickpay subscription payment or a simple non-recurring payment, don't care.

        If first capture didn't succeed the subscription is created/renewed if the capture succeeds later

        TODO: consider making a setting for subscribe on authorized or on captured
        Currently subscriptions are always auto-capture, so it makes sense to always subscribe on capture
        """
        order_item: Optional['OrderItem'] = instance.get_subscription_item()
        if order_item is not None:
            logging.debug("payment_quickpay: subscribe_on_order_captured(), subscription paid, order id={}"
                          .format(instance.pk))
            # Create the Subscription
            username = instance.username or getattr(instance, 'reference', '')
            
            subscription = Subscription.subscribe(username, order_item.sku, getattr(order_item, 'currency', 'USD'),
                user_email=instance.billing_detail_email, membership_id=instance.membership_id, order=instance)
            subscription.is_authorized = bool(instance.membership_id)
            subscription.save()
            
            status_paid = getattr(settings, 'QUICKPAY_ORDER_STATUS_PAID', None)
            if status_paid:
                logging.debug("payment_quickpay: subscribe_on_order_captured(), setting status to paid")
                instance.status = status_paid
            instance.save()
            subscription_paid.send(sender=SubscriptionPeriod, instance=subscription)
        else:
            logging.debug("payment_quickpay: subscribe_on_order_captured(), order id={} is not a subscription order"
                          .format(instance.pk))
