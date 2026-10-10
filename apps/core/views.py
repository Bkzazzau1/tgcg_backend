from django.db import connection
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """Liveness/readiness probe: confirms the API and database respond."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(responses={200: dict, 503: dict})
    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            database = "ok"
        except Exception:  # noqa: BLE001 - any DB failure means unhealthy
            database = "unavailable"
        healthy = database == "ok"
        return Response(
            {"status": "ok" if healthy else "degraded", "database": database},
            status=200 if healthy else 503,
        )
