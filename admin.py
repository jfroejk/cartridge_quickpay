import django
from django.contrib import admin
from .models import QuickpayPayment


class QuickpayPaymentAdmin(admin.ModelAdmin):
    list_display = ['qp_id', 'order_id', 'requested_amount', 'requested_currency', 'accepted', 'test_mode', 'state', 'balance',
                    'accepted_date', 'captured_date']

    def order_id(self, instance: QuickpayPayment):
        return instance.pk


admin.site.register(QuickpayPayment, QuickpayPaymentAdmin)
