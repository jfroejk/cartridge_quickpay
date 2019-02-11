"""
QuickPay payments
"""
import logging
from decimal import Decimal
from django.db import models
from django.core.exceptions import ImproperlyConfigured
from django.utils.timezone import now
from django.conf import settings
from cartridge.shop.models import Order
from cartridge.shop.checkout import CheckoutError
from quickpay_api_client import QPClient
from quickpay_api_client.exceptions import ApiError

from datetime import datetime
try:
    from typing import Optional
except ImportError:
    Optional = None


__author__ = 'jfk@metation.dk'


def quickpay_client() -> QPClient:
    """Get QuickPay client proxy object"""
    if not getattr(settings, 'QUICKPAY_API_KEY', ''):
        raise ImproperlyConfigured("QUICKPAY_API_KEY missing or empty in settings")
    secret = ":{0}".format(settings.QUICKPAY_API_KEY)
    return QPClient(secret)


class QuickpayPayment(models.Model):
    order = models.ForeignKey(Order)
    # Use integer for requested_amount because QuickPay operates in minor units (cents, øre)
    requested_amount = models.IntegerField()  # type: int
    requested_currency = models.CharField(max_length=3)    # type: str
    card_last4 = models.CharField(max_length=4, help_text="Last 4 digits of card number")  # type: str

    qp_id = models.IntegerField(null=True, db_index=True, help_text="ID of Payment in Quickpay")  # type: int
    accepted = models.BooleanField(default=False)          # type: bool
    test_mode = models.BooleanField(default=True)          # type: bool
    type = models.CharField(null=True, max_length=31)      # type: str
    text_on_statement = models.TextField(null=True)        # type: str
    acquirer = models.CharField(null=True, max_length=31)  # type: str
    state = models.CharField(null=True, max_length=31)     # type: str
    balance = models.IntegerField(null=True)               # type: int

    accepted_date = models.DateTimeField(null=True)        # type: datetime
    captured_date = models.DateTimeField(null=True)        # type: datetime
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
                                 requested_currency=currency, card_last4=card_last4)
        return res

    @property
    def may_capture(self) -> bool:
        """Whether payment may be captured"""
        return bool(self.accepted_date and self.captured_date is None)

    def capture(self, amount: 'Optional[Decimal]'=None) -> bool:
        """Capture this payment. May only capture once. Extra capture() calls have no effect.

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
        client = quickpay_client()
        try:
            client.post('/payments/%s/capture' % self.qp_id, **{'amount': int_amount})
            # print("capture res", qp_res)
            self.captured_date = now()
            # self.update_from_res(qp_res)
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
        client = quickpay_client()
        try:
            # print("Attempt to refund %d" % int_amount)
            client.post('/payments/%s/refund' % self.qp_id, **{'amount': int_amount})
            # print("RES", qp_res)
            # self.update_from_res(qp_res)
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
        client = quickpay_client()
        try:
            qp_res = client.get('/payments/%s' % self.qp_id)
        except ApiError as e:
            logging.error("QuickPay API error: %s" % e.body)
            return
        # print("qp res=", qp_res)
        self.update_from_res(qp_res)

    def update_from_res(self, res: dict):
        """Update payment data from QuickPay result"""
        self.qp_id = res['id']
        self.accepted = res['accepted']
        self.test_mode = res['test_mode']
        self.type = res['type']
        self.text_on_statement = res['text_on_statement'] or ''
        self.acquirer = res['acquirer']
        self.state = res['state']
        self.balance = res['balance']
