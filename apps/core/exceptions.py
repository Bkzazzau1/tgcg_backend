from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.views import exception_handler as drf_exception_handler


def exception_handler(exc, context):
    """Map model/service ValidationErrors to DRF 400 responses."""
    if isinstance(exc, DjangoValidationError):
        detail = exc.message_dict if hasattr(exc, "error_dict") else {"detail": exc.messages}
        detail = {("detail" if key == "__all__" else key): value for key, value in detail.items()}
        exc = serializers.ValidationError(detail)
    return drf_exception_handler(exc, context)
