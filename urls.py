from django.conf.urls import url
from .views import *

urlpatterns = [
    url("^checkout/$", checkout_quickpay, name='quickpay_checkout'),
    url("^callback/$", callback, name='quickpay_callback'),
    url("^success/$", success, name='quickpay_success'),
    url("^success_framed/$", success_framed, name='quickpay_success_framed'),
    url("^failed/$", failed, name='quickpay_failed'),
    url("^failed_framed/$", failed_framed, name='quickpay_failed_framed'),
]
