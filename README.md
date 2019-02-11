*Work in progress...*

- Payment window + callback vs. quickpay_payment_handler()

## Quickpay settings

Settings > Integration > Callback URL must be set to the callback URL of cartridge_quickpay, e.g.
https://myshop.com/quickpay/callback/. Otherwise Quickpay won't make callbacks and paymnent information
won't get registered properly in the shop.


## Possible improvements

- Better handling of accept and capture callbacks. Record accept date on accept callback and capture date or capture
callback.