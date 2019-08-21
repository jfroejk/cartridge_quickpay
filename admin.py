import django
from django.core.urlresolvers import reverse
from django.contrib import admin
from .models import QuickpayPayment


try:
    from cartridge_subscription.models import Subscription, SubscriptionPeriod
except ImportError:
    Subscription, SubscriptionPeriod = None, None
    

class QuickpayPaymentAdmin(admin.ModelAdmin):
    list_display = ['qp_id', 'shop_order', 'requested_amount', 'requested_currency', 'accepted',
                    'state', 'balance', 'accepted_date', 'captured_date', 'test_mode']
    list_select_related = ('order',)
        
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

    def subscription(self, item: QuickpayPayment):
        try:
            subscription_id = item.order.subscriptionperiod.subscription_id
        except (AttributeError, SubscriptionPeriod.DoesNotExist):
            subscription_id = None
        if subscription_id is not None:
            admin_url = reverse(
                "admin:%s_%s_change"
                % (Subscription._meta.app_label, Subscription._meta.model_name), args=(subscription_id,))

            return "<a href='{}'>{}</a>".format(admin_url, subscription_id)
        else:
            return "-"

    subscription.allow_tags = True


if Subscription is not None:
    QuickpayPaymentAdmin.list_select_related = QuickpayPaymentAdmin.list_select_related + ('order__subscriptionperiod',)
    QuickpayPaymentAdmin.list_display[2:2] = ['subscription']


admin.site.register(QuickpayPayment, QuickpayPaymentAdmin)
