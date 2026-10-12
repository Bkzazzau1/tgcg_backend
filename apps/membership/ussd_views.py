"""The USSD provider's callback (see apps.membership.ussd)."""

import hmac
import logging
import traceback

from django.conf import settings
from django.http import Http404, HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from . import ussd

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class UssdCallbackView(View):
    """Answers each step of a USSD session with the next screen.

    The secret token in the URL is shared only with the provider; without one
    configured, USSD is off. Restrict the path to the provider's addresses at
    the web server as well.
    """

    http_method_names = ["post"]

    def post(self, request, token):
        expected = settings.USSD_CALLBACK_TOKEN
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            raise Http404
        try:
            screen = ussd.respond(
                phone=request.POST.get("phoneNumber", ""),
                session_id=request.POST.get("sessionId", ""),
                service_code=request.POST.get("serviceCode", ""),
                text=request.POST.get("text", ""),
            )
        except Exception as error:
            # The request holds the member's PIN and the error may hold their
            # phone number: log where it failed, never what was sent.
            frame = traceback.extract_tb(error.__traceback__)[-1]
            logger.error(
                "USSD request failed: %s at %s:%s",
                type(error).__name__,
                frame.filename,
                frame.lineno,
            )
            screen = ussd.Screen("Sorry, TGCG could not finish this. Please try again later.", True)
        return HttpResponse(screen.render(), content_type="text/plain; charset=utf-8")
