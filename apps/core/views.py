"""Class-based views for core platform pages.

All application pages use class-based views. Only framework callbacks are
allowed to use function-based views.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views import View
from django.views.generic import TemplateView


class HomeView(TemplateView):
    """The site root: dashboard when signed in, the sign-in page otherwise."""

    template_name = "core/home.html"

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            # Render (and process) the sign-in page at the site root so a
            # signed-out visitor's default page is the standalone login screen.
            from apps.accounts.views import LoginView

            return LoginView.as_view()(request, *args, **kwargs)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["features"] = [
            {
                "icon": "clipboard-list",
                "title": "Configurable checklists",
                "description": "Versioned templates with sections, items, answer types and rules.",
            },
            {
                "icon": "building-2",
                "title": "Multi-organization",
                "description": "Scoped data across organizations, locations and hierarchies.",
            },
            {
                "icon": "users",
                "title": "Role-based access",
                "description": "Data-driven roles and an explicit authorization boundary.",
            },
            {
                "icon": "wifi-off",
                "title": "Offline capable",
                "description": "Installable PWA with an offline shell and sync foundation.",
            },
            {
                "icon": "braces",
                "title": "REST API",
                "description": "Versioned DRF API with a consistent response envelope.",
            },
            {
                "icon": "shield-check",
                "title": "Secure by default",
                "description": "Strict CSP, nonce-scoped scripts and hardened defaults.",
            },
        ]
        return context


class OfflineView(TemplateView):
    """Offline fallback page served by the service worker."""

    template_name = "pwa/offline.html"


class ManifestView(TemplateView):
    """Serve the web app manifest from the root URL space."""

    template_name = "pwa/manifest.webmanifest"
    content_type = "application/manifest+json"


class ServiceWorkerView(View):
    """Serve the service worker from the site root so it controls all routes."""

    def get(self, request: HttpRequest) -> HttpResponse:
        source = settings.BASE_DIR / "static" / "js" / "service-worker.js"
        try:
            content = source.read_text(encoding="utf-8")
        except FileNotFoundError:  # pragma: no cover - defensive
            return HttpResponse(status=404)
        response = HttpResponse(content, content_type="application/javascript")
        response["Service-Worker-Allowed"] = "/"
        response["Cache-Control"] = "no-cache"
        return response


class HealthCheckView(View):
    """Liveness/readiness probe used by load balancers and monitoring."""

    def get(self, request: HttpRequest) -> JsonResponse:
        payload = self.build_payload()
        return JsonResponse(payload, status=200 if payload["status"] == "ok" else 503)

    @classmethod
    def build_payload(cls) -> dict:
        database = cls._check_database()
        tasks = cls._check_tasks()
        overall = "ok" if database["status"] == "ok" else "degraded"
        return {
            "status": overall,
            "version": getattr(settings, "APP_VERSION", "0.1.0"),
            "environment": getattr(settings, "ENVIRONMENT", "development"),
            "checks": {"database": database, "tasks": tasks},
        }

    @classmethod
    def _check_database(cls) -> dict[str, str]:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:  # pragma: no cover - depends on infra
            return {"status": "error", "detail": str(exc)}
        return {"status": "ok"}

    @staticmethod
    def _check_tasks() -> dict[str, str]:
        backend = settings.TASKS.get("default", {}).get("BACKEND", "")
        return {"status": "ok", "backend": backend}


class HealthWidgetView(TemplateView):
    """HTMX fragment rendering the health status card on the landing page."""

    template_name = "partials/health_status.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        payload = HealthCheckView.build_payload()
        context["health"] = {
            "ok": payload["status"] == "ok",
            "version": payload["version"],
            "environment": payload["environment"],
            "checks": payload["checks"],
        }
        return context
