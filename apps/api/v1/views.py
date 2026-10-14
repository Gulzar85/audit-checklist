"""API v1 views."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import connection
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import APIRootSerializer, HealthSerializer


class APIRootView(APIView):
    """Discovery endpoint listing the API version and available routes."""

    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []
    serializer_class = APIRootSerializer

    @extend_schema(responses=APIRootSerializer)
    def get(self, request: Request) -> Response:
        return Response(
            {
                "version": "v1",
                "endpoints": {
                    "health": str(request.build_absolute_uri("health/")),
                    "schema": str(request.build_absolute_uri("/api/schema/")),
                },
            }
        )


class HealthCheckView(APIView):
    """API health probe (unauthenticated, no throttling)."""

    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []
    throttle_classes: list[Any] = []
    serializer_class = HealthSerializer

    @extend_schema(responses=HealthSerializer)
    def get(self, request: Request) -> Response:
        database_ok = self._database_ok()
        payload = {
            "status": "ok" if database_ok else "degraded",
            "version": getattr(settings, "APP_VERSION", "0.1.0"),
            "environment": getattr(settings, "ENVIRONMENT", "development"),
            "checks": {"database": {"status": "ok" if database_ok else "error"}},
        }
        status_code = 200 if database_ok else 503
        return Response(payload, status=status_code)

    @staticmethod
    def _database_ok() -> bool:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:  # pragma: no cover - infra dependent
            return False
        return True
