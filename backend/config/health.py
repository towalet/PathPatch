"""Liveness/readiness endpoint used by Docker and hosting platforms."""

from __future__ import annotations

from django.db import connection
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView


class _HealthThrottle(AnonRateThrottle):
    scope = "anon"


class HealthCheckView(APIView):
    """Report service and database readiness without requiring auth."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes: list = []

    def get(self, request: Request) -> Response:
        db_ok = True
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:  # pragma: no cover - exercised only on db outage
            db_ok = False

        status_code = 200 if db_ok else 503
        return Response(
            {
                "status": "ok" if db_ok else "degraded",
                "service": "patchpath-api",
                "database": "ok" if db_ok else "unavailable",
            },
            status=status_code,
        )


health_check = HealthCheckView.as_view()
