from django.core.management.base import BaseCommand
from cartridge.shop.models import Order
from cartridge_quickpay.payment import get_quickpay_link


class Command(BaseCommand):
    help = 'Get order links for given orders'

    def add_arguments(self, parser):
        parser.add_argument('orders', nargs='*', type=str)

    def handle(self, *args, **options):
        for order_no in options['orders']:
            order: Order = Order.objects.get(pk=order_no)
            print("Quickpay link:", get_quickpay_link(order))
