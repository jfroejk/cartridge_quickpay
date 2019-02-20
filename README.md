*Work in progress...*

## Introduction

Cartridge is a simple, yet powerful web shop module for Django and Mezzanine. Mezzanine lets you add CMS and blog
functionality to your Django projects and Cartridge lets you add web shop functionality for B2C as well as B2B.

Quickpay is a payment service provider based in Denmark available to all EU based businesses. It supports major
credit cards, Paypal, Apple Pay, Dankort (Denmark), and Mobilepay (Denmark and Finland).

Cartridge_quickpay makes it easy to add Quickpay payment to a Cartridge based shop.


## Modes of operation

Quickpay offers two distinct modes of operation: payment window mode and embedded mode.

Payment window mode displays Quickpay's payment window in a Bootstrap modal or in the full browser window. It supports
all acquirers and payment methods.

Embedded mode lets you use the payment form in Cartridge with you own payment form design. It supports credit card
payments but not other payment methods, such as Mobilepay. While it is possible to combine payment window mode and
embedded mode, doing so is not recommended and is not supported out-of-the-box in cartridge_quickpay.

## Order statuses

Orders in Cartridge have a status field describing its current stage in the ordering/payment process. The default
statuses are very limited. To be able to use the status field to track the order's progress, a few new statuses
are defined in `settings.py`:

```python
# Define order statuses
ORDER_STATUS_NEW = 1
ORDER_STATUS_AUTHORIZED = 5
ORDER_STATUS_WAITING = 10
ORDER_STATUS_DONE = 20
ORDER_STATUS_BILLED = 30
ORDER_STATUS_PAID = 40

SHOP_ORDER_STATUS_CHOICES = (
    (ORDER_STATUS_NEW, "New"),         # Order received, not yet processed
    (ORDER_STATUS_AUTHORIZED, "Payment authorized"),  # Authorized, complete() not called
    (ORDER_STATUS_WAITING, "Waiting"), # Order waiting to be fulfilled
    (ORDER_STATUS_BILLED, "Billed"),   # Order billed
    (ORDER_STATUS_PAID, "Paid"),       # Payment drawn or received
)


QUICKPAY_ORDER_STATUS_AUTHORIZED = ORDER_STATUS_AUTHORIZED
QUICKPAY_ORDER_STATUS_WAITING = ORDER_STATUS_WAITING
```

## Order handler and Quickpay settings

```python
SHOP_HANDLER_ORDER = 'cartridge_quickpay.payment.order_handler'
SHOP_HANDLER_PAYMENT = 'cartridge.shop.checkout.default_payment_handler'  # we use Quickpay's payment window, no payment handler!

QUICKPAY_FRAMED_MODE = True  # True for Bootstrap modal payment window, False for full browser window
QUICKPAY_ORDER_FORM = <dotted path to order form. defaults to 'cartridge.shop.forms'>
QUICKPAY_FAILED_URL = <URL to redirect to if payment failed or cancelled>

QUICKPAY_SHOP_BASE_URL = <base URL of the shop for success, cancel and callback URLs>
QUICKPAY_API_KEY = <private key from Quickpay>
QUICKPAY_PRIVATE_KEY = <API key from Quickpay>
QUICKPAY_AUTO_CAPTURE = False  # Whether to auto-capture when purchase done
QUICKPAY_TESTMODE = True       # Whether to let payments with test cards through

```

You find your Quickpay API key and private key in the Quickpay management interface. The private key is in Settings >
Mercant > Mercant Settings - Private key. The API key is in Settings > Integration > API User - API key.

## Integration of payment window

To use the Quickpay payment window, the standard payment form must be disabled and replaced with activation of the
Quickpay payment window.

Make a copy of `shop/includes/checkout.html` in your project and do these edits:

In block `nav-buttons`, replace:

```html
<input type="submit" class="btn btn-lg btn-primary pull-right" value="{% trans "Next" %}">
``` 

with

```html
<button id="checkout-quickpay-btn" class="btn btn-lg btn-primary pull-right">Make payment</button> 
```

Add Quickpay payment window setup at the bottom:


```html
{% block footer_js %}
{{ block.super }}
{% load cartridge_quickpay_tags %}
{% quickpay_payment_window %}
{% endblock %}
```

## Using Quickpay embedded


## Settings in Quickpay

Settings > Integration > Callback URL must be set to the callback URL of cartridge_quickpay, e.g.
https://myshop.com/quickpay/callback/. Otherwise Quickpay won't make callbacks and paymnent information
won't get registered properly in the shop.


## Possible improvements

- Better handling of accept and capture callbacks. Record accept date on accept callback and capture date or capture
callback.