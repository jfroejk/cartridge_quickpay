from django import template


register = template.Library()


@register.inclusion_tag("cartridge_quickpay/payment_window.html")
def quickpay_payment_window() -> dict:
    return {}
