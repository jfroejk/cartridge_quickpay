"""
QuickPay payments
"""
import logging
from decimal import Decimal
from django.db import models
from django.core.exceptions import ImproperlyConfigured
from django.dispatch import receiver
from django.db.models.signals import post_delete
from django.utils.timezone import now
from django.conf import settings
from cartridge.shop.models import Order, OrderItem, Product
from cartridge.shop.checkout import CheckoutError
from cartridge.shop import fields
from quickpay_api_client import QPClient
from quickpay_api_client.exceptions import ApiError

from datetime import datetime
try:
    from typing import Optional
except ImportError:
    Optional = None


__author__ = 'jfk@metation.dk'


def quickpay_client(currency: Optional[str] = None) -> QPClient:
    """Get QuickPay client proxy object"""
    secret = ":{0}".format(get_api_key(currency))
    return QPClient(secret)


def get_api_key(currency: Optional[str] = None) -> str:
    """Get API key for the agreement for the given currency"""
    try:
        return settings.QUICKPAY_API_KEY
    except AttributeError:
        raise ImproperlyConfigured("QUICKPAY_API_KEY missing or empty in settings")


def get_private_key(currency: Optional[str] = None) -> str:
    """Get private key for the agreement for the given currency"""
    try:
        return settings.QUICKPAY_PRIVATE_KEY
    except AttributeError:
        raise ImproperlyConfigured("QUICKPAY_PRIVATE_KEY missing or empty in settings")
    

class QuickpayPayment(models.Model):
    order = models.ForeignKey(Order, editable=False)
    # When an order is deleted, associated payments are deleted. If possible, they are cancelled in Quickpay
    # by post_delete handler
    
    # Uses integer for requested_amount because QuickPay operates in minor units (cents, øre)
    requested_amount = models.IntegerField(editable=False,
        help_text="Requested amount in minor unit, e.g. cent. "
                  "NB: for subscriptions this is the period amount with tax. "
                  "The captured amount may be smaller if the previous period was (partly) refunded." )  # type: int
    requested_currency = models.CharField(max_length=3, editable=False)    # type: str
    card_last4 = models.CharField(max_length=4, editable=False, help_text="Last 4 digits of card number")  # type: str

    qp_id = models.IntegerField(
        null=True, db_index=True, editable=False, help_text="ID of Payment in Quickpay")  # type: int
    accepted = models.BooleanField(default=False, editable=False)          # type: bool
    test_mode = models.BooleanField(default=True, editable=False)          # type: bool
    type = models.CharField(null=True, max_length=31, editable=False)      # type: str
    text_on_statement = models.TextField(null=True, editable=False)        # type: str
    acquirer = models.CharField(null=True, max_length=31, editable=False)  # type: str
    state = models.CharField(null=True, max_length=31, editable=False)     # type: str
    balance = models.IntegerField(null=True, editable=False,
        help_text="Captured amount in minor unit, e.g. cent")              # type: int

    last_qp_status = models.CharField(null=True, max_length=31, editable=False,
        help_text="Last status code from Quickpay")                        # type: str
    last_qp_status_msg = models.CharField(null=True, max_length=255, editable=False,
        help_text="Last status message from Quickpay")                     # type: str
    last_aq_status = models.CharField(null=True, max_length=31, editable=False,
        help_text="Last status code from acquirer")                        # type: str
    last_aq_status_msg = models.CharField(null=True, max_length=255, editable=False,
        help_text="Last status message from acquirer")                     # type: str
    
    accepted_date = models.DateTimeField(null=True, editable=False)        # type: datetime
    captured_date = models.DateTimeField(null=True, editable=False)        # type: datetime
    # Only known if the payment has been captured through cartridge_quickpay. Unknown if autocaptured

    class Meta:
        ordering = ['order']

    @classmethod
    def create_card_payment(cls, order: Order, amount: Decimal, currency: str, card_last4: str) -> 'QuickpayPayment':
        """Create new payment attempt for Order. Fail if order already paid

        # Args:
        order : Order = Order to pay
        amount : Decimal = Order amount
        currency : string = The currency of the payment
        card_last4 : string = Last 4 digits of card number
        """
        assert isinstance(order, Order)
        assert isinstance(amount, Decimal)
        succeeded_payment = cls.objects.filter(order=order, accepted=True)
        if succeeded_payment:
            raise CheckoutError("Order already paid!")
        int_amount = int(amount * 100)
        res = cls.objects.create(order=order, requested_amount=int_amount,
                                 requested_currency=currency, card_last4=card_last4, state='new')
        return res

    @classmethod
    def get_order_payment(cls, order: Order, lock: bool = True) -> Optional['QuickpayPayment']:
        """Get the latest payment associated with the Order. Lock it for update.
        Return None if no payment found"""
        payments = order.quickpaypayment_set.all().order_by('-id')[:1]
        if lock:
            payments = payments.select_for_update()
        return payments[0] if payments else None
    
    @property
    def is_accepted(self) -> bool:
        return bool(self.accepted_date)

    @property
    def is_captured(self) -> bool:
        return bool(self.captured_date)
    
    @property
    def may_capture(self) -> bool:
        """Whether payment may be captured"""
        return bool(self.accepted_date and self.captured_date is None)

    def capture(self, amount: 'Optional[Decimal]'=None) -> bool:
        """Capture this payment. May only capture once. Extra capture() calls have no effect.
        TODO: this function hasn't been used much for newer versions of Quickpay. Needs test and correction.

        # Args:
        amount : Decimal | None = Decimal amount, default requested amount

        # Returns bool = Whether capture succeeded.
        SHOULD return an error code to tell the user what went wrong!
        """
        assert amount is None or isinstance(amount, Decimal)
        self.update_from_quickpay()  # Make sure we have the latest data form QP
        if amount is not None:
            assert isinstance(amount, Decimal)
            int_amount = min(self.requested_amount, int(amount * 100))
        else:
            int_amount = self.requested_amount
        client = quickpay_client(self.requested_currency)
        try:
            client.post('/payments/%s/capture' % self.qp_id, **{'amount': int_amount})
            # print("capture res", qp_res)
            self.captured_date = now()
            # Have to get object again, the returned object is with the old data
            self.update_from_quickpay()
            res = True
        except ApiError as e:
            logging.error("QuickPay API error: %s" % e.body)
            res = False
        self.save()
        return res

    def refund(self, amount: 'Optional[Decimal]'=None) -> bool:
        """Refund this payment

        # Args:
        amount : Decimal | None = Decimal amount, default captured amount

        # Returns bool = Whether refund succeeded.
        SHOULD return an error code to tell the user what went wrong!
        """
        assert amount is None or isinstance(amount, Decimal)
        self.update_from_quickpay()  # Make sure we have the latest data form QP
        if amount is not None:
            assert isinstance(amount, Decimal)
            int_amount = min(self.balance, int(amount * 100))
        else:
            int_amount = self.balance
        client = quickpay_client(self.requested_currency)
        try:
            # print("Attempt to refund %d" % int_amount)
            client.post('/payments/%s/refund' % self.qp_id, **{'amount': int_amount})
            # print("RES", qp_res)
            # Have to get object again, the returned object is with the old data
            self.update_from_quickpay()
            if self.balance == 0:
                self.captured_date = None
            res = True
        except ApiError as e:
            logging.error("QuickPay API error: %s" % e.body)
            res = False
        self.save()
        return res

    def update_from_quickpay(self):
        """Update data from QuickPay"""
        if self.qp_id is None:
            return
        client = quickpay_client(self.requested_currency)
        try:
            qp_res = client.get('/payments/%s' % self.qp_id)
        except ApiError as e:
            logging.error("QuickPay API error: %s" % e.body)
            return
        # print("qp res=", qp_res)
        self.update_from_res(qp_res)

    def update_from_res(self, res: dict):
        """Update payment data from QuickPay result. Doesn't save"""
        self.qp_id = res['id']
        self.accepted = res['accepted']
        self.test_mode = res['test_mode']
        self.type = res['type']
        self.text_on_statement = res['text_on_statement'] or ''
        self.acquirer = res['acquirer']
        self.state = res['state']
        self.balance = res.get('balance', 0)
        self.card_last4 = res.get('metadata', {}).get('last4', self.card_last4) or '9999'

        operations = res.get('operations', [])
        if operations:
            last_op = operations[-1]
            self.last_qp_status = last_op['qp_status_code']
            self.last_qp_status_msg = last_op['qp_status_msg']
            self.last_aq_status = last_op['aq_status_code']
            self.last_aq_status_msg = last_op['aq_status_msg']

        if self.accepted:
            timestamp = now()
            if not self.accepted_date:
                self.accepted_date = timestamp
            if self.state == 'processed' and not self.captured_date:
                self.captured_date = timestamp


@receiver(post_delete, sender=QuickpayPayment)
def _quickpay_payment_post_delete(sender, instance: QuickpayPayment, **kwargs):
    """Delete payment link in Quickpay if it hasn't been accepted.
    The payment itself cannot be deleted."""
    from .payment import delete_payment_link
    delete_payment_link(instance)


@receiver(post_delete, sender=Order)
def _order_post_delete_subscription(sender, instance: Order, **kwargs):
    """Delete subscription in Quickpay if order deleted before subscription activated/paid.
    """
    # Applicable when subscription has been created in Quickpay but not in cartridge_subscription.
    # cartridge_subscription.Subscription is created when the subscription has been paid!
    from .payment import delete_order_subscription
    delete_order_subscription(instance)
