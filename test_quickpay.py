__author__ = 'jfk'

# Simple tests of Quickpay integration
# Test kort: http://tech.quickpay.net/appendixes/test/

import django; django.setup()
from quickpay_api_client import QPClient
from cartridge_quickpay.payment_handler import get_quickpay_link
from cartridge.shop.models import Order
import sys


# Link test with given order id
order = Order.objects.get(id=16)
print(get_quickpay_link(order))

sys.exit()

# Key for merchant jfk@metation.dk
secret = ":{0}".format('e7b41e873c3c8829141b8e4bc885f3ab289feee37df855525f3d6ec2e7898026')
client = QPClient(secret)

res = client.post('/payments', currency='DKK', order_id='test_10008')
payment_id = res['id']
card = {'number' : '1000020000000006', 'expiration' : '1609', 'cvd':'123'}
print (client.post('/payments/%s/authorize' % payment_id,
                   **{'amount':1.00, 'card' : card, 'card[number]':'1000 0200 0000 0006',
                   'acquirer' : 'nets'}
                   ))

#print (client.get('/ping'))
#print (client.post('/fees', amount=1.00))
#print (client.get('/operational-status/aquirers'))
#client = QPClient()
#print (client.get('/ping')) # Only anonymous may do this?