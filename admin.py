import django
from django.core.urlresolvers import reverse
from django.contrib import admin
from .models import QuickpayPayment


class QuickpayPaymentAdmin(admin.ModelAdmin):
    list_display = ['qp_id', 'shop_order', 'requested_amount', 'requested_currency', 'accepted', 'test_mode',
                    'state', 'balance', 'accepted_date', 'captured_date']

    search_fields = ['order__username', 'order__reference', 'order__billing_detail_email',
                     'order__membership_id', 'qp_id']

    list_filter = ['state', 'accepted_date', 'accepted', 'test_mode']
    
    readonly_fields = ['qp_id', 'shop_order', 'requested_amount', 'requested_currency', 'accepted', 'test_mode',
                       'type', 'text_on_statement', 'acquirer', 'state', 'balance',
                       'last_qp_status', 'last_qp_status_msg', 'last_aq_status', 'last_aq_status_msg',
                       'accepted_date', 'captured_date']

    def has_add_permission(self, request):
        return False

    def shop_order(self, item: QuickpayPayment):
        from cartridge.shop.models import Order
        order_id = item.order_id
        if order_id is not None:
            admin_url = reverse("admin:%s_%s_change"
                                    % (Order._meta.app_label, Order._meta.model_name), args=(order_id,))

            return "<a href='{}'>{}</a>".format(admin_url, order_id)
        else:
            return "-"

    shop_order.allow_tags = True


admin.site.register(QuickpayPayment, QuickpayPaymentAdmin)
