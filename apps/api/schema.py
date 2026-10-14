"""OpenAPI schema views (drf-spectacular)."""

from __future__ import annotations

from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.permissions import AllowAny


class APISchemaView(SpectacularAPIView):
    """Serve the OpenAPI 3 schema without URL version negotiation."""

    versioning_class = None
    permission_classes = [AllowAny]
    urlconf = None


class APISwaggerView(SpectacularSwaggerView):
    """Serve interactive API documentation."""

    versioning_class = None
    permission_classes = [AllowAny]
