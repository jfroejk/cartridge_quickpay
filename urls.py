from django.conf.urls import url
from .views import *


urlpatterns = [
    url("^checkout/$", quickpay_checkout, name='quickpay_checkout'),
    url("^callback/$", callback, name='quickpay_callback'),
    url("^success/$", success, name='quickpay_success'),
    url("^failed/$", failed, name='quickpay_failed'),
]
