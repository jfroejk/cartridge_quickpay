from django.http import HttpRequest, HttpResponse
from django.utils.deprecation import MiddlewareMixin
from typing import Optional
import logging


class QuickpayMiddleware(MiddlewareMixin):
    def process_view(self, request: HttpRequest, view_func, view_args, view_kwargs) -> Optional[HttpResponse]:
        from cartridge.shop.views import checkout_steps
        from cartridge.shop.checkout import CHECKOUT_STEP_FIRST
        logging.debug("Quickpay.process_view: method={}, at checkout={}, step={}"
                      .format(request.method, view_func is checkout_steps, request.POST.get('step', 0)))
        step_str = request.POST.get('step', '0')
        step = int(step_str) if step_str.isdigit() else 0
        if (request.method == 'POST'
                and view_func is checkout_steps
                and step == CHECKOUT_STEP_FIRST):
            logging.debug("Quickpay.process_view: Making QP checkout view")
            from .views import quickpay_checkout
            return quickpay_checkout(request)
        else:
            return None

            
