__author__ = 'jfk'
from mezzanine.conf import register_setting

register_setting(
    name="QUICKPAY_API_KEY",
    label="API key for QuickPay",
    description="The API key used to access QuickPay.",
    editable=False,
    default="",
)

register_setting(
    name="QUICKPAY_ACQUIRER",
    label="Acquirer for QuickPay",
    description="The acquirer QuickPay will use to draw payments.",
    editable=False,
    default="nets",
)

register_setting(
    name="QUICKPAY_TESTMODE",
    label="Whether to operate in test mode",
    description="If in test mode, payments with test card are accepted as valid, otherwise rejected.",
    editable=False,
    default=True,
)
