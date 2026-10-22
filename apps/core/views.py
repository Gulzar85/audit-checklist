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

from .mixins import PolicyMixin
from .policies import is_platform_staff


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
        context.update(self._dashboard())
        return context

    def _dashboard(self) -> dict:
        """Real, tenant-scoped figures from the domains that exist today (no placeholders)."""
        from django.db.models import Count, Q

        from apps.accounts.context import get_active_organization
        from apps.locations.selectors import location_types_visible_to, locations_visible_to
        from apps.organizations.selectors import organizations_visible_to

        from .selectors import recent_activity
        from .timeline import group_activity

        user = self.request.user
        locations = locations_visible_to(user)
        totals = locations.aggregate(total=Count("id"), active=Count("id", filter=Q(is_active=True)))
        by_type = list(
            locations.values("location_type__name").annotate(n=Count("id")).order_by("-n", "location_type__name")[:6]
        )
        organization = get_active_organization(self.request)
        return {
            "organization_count": organizations_visible_to(user).count(),
            "location_total": totals["total"],
            "location_active": totals["active"],
            "location_inactive": totals["total"] - totals["active"],
            "location_type_count": location_types_visible_to(user).count(),
            "locations_by_type": [
                {"name": row["location_type__name"], "count": row["n"],
                 "percent": round(100 * row["n"] / totals["total"]) if totals["total"] else 0}
                for row in by_type
            ],
            "active_percent": round(100 * totals["active"] / totals["total"]) if totals["total"] else 0,
            "activity_days": group_activity(recent_activity(organization_id=organization.pk, limit=10)) if organization else [],
            "dashboard_organization": organization,
        }


class OfflineView(TemplateView):
    """Offline fallback page served by the service worker."""

    template_name = "pwa/offline.html"


class OfflineAppView(TemplateView):
    """The public, data-free offline application shell (precached by the service worker; see templates/pwa/offline_app.html)."""

    template_name = "pwa/offline_app.html"

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:  # noqa: ANN002, ANN003
        response = super().get(request, *args, **kwargs)
        response["Cache-Control"] = "no-cache"  # always revalidated; the worker keeps its own precached copy for offline use
        return response


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



class GlobalSearchView(PolicyMixin, TemplateView):
    """HTMX fragment behind the command palette: grouped results across the domains that exist.

    Scoped through the same selectors as every list (``organizations_visible_to`` /
    ``locations_visible_to``), so a result can never name something the caller cannot open.  New
    modules add a group here; the palette markup does not change.
    """

    template_name = "components/navigation/search_results.html"
    MIN_LENGTH = 2
    LIMIT = 5

    def policy_allows(self, obj=None) -> bool:  # noqa: ANN001
        return True  # any signed-in user; results are scoped, not gated

    def get_context_data(self, **kwargs):  # noqa: ANN003
        from django.db.models import Q
        from django.urls import reverse

        from apps.locations.selectors import locations_visible_to
        from apps.organizations.selectors import organizations_visible_to

        context = super().get_context_data(**kwargs)
        query = (self.request.GET.get("q") or "").strip()[:80]
        context["query"] = query
        context["too_short"] = len(query) < self.MIN_LENGTH
        groups = []
        if not context["too_short"]:
            user = self.request.user
            organizations = organizations_visible_to(user).filter(Q(name__icontains=query) | Q(code__icontains=query))[: self.LIMIT]
            locations = (
                locations_visible_to(user)
                .filter(Q(name__icontains=query) | Q(code__icontains=query))
                .select_related("organization", "location_type")
                .order_by("name")[: self.LIMIT]
            )
            groups = [
                {"label": "Organizations", "icon": "building-2", "items": [
                    {"label": o.name, "meta": o.code, "url": o.get_absolute_url()} for o in organizations]},
                {"label": "Locations", "icon": "network", "items": [
                    {"label": l.name, "meta": f"{l.location_type.name} - {l.organization.name}",
                     "url": reverse("locations:detail", args=[l.pk])} for l in locations]},
            ]
            groups = [g for g in groups if g["items"]]
        context["groups"] = groups
        return context


class UIShowcaseView(PolicyMixin, TemplateView):
    """The developer component library (platform staff only - see docs/ui-design-system.md)."""

    template_name = "pages/ui/showcase.html"

    def policy_allows(self, obj=None) -> bool:  # noqa: ANN001
        return is_platform_staff(self.request.user)

    def get_context_data(self, **kwargs):  # noqa: ANN003
        context = super().get_context_data(**kwargs)
        context["sample_rows"] = [
            {"name": "Safety inspection", "version": "v3", "status": "published", "updated": "Today"},
            {"name": "Cleaning round", "version": "v2", "status": "draft", "updated": "Yesterday"},
            {"name": "Food safety audit", "version": "v5", "status": "published", "updated": "2 days ago"},
            {"name": "Equipment check", "version": "v1", "status": "archived", "updated": "Last week"},
        ]
        context["sample_tree"] = [
            {"label": "Region", "meta": "Country", "children": [
                {"label": "North", "meta": "Area", "children": [
                    {"label": "Site 101", "meta": "Site", "children": []},
                    {"label": "Site 102", "meta": "Site", "children": []},
                ]},
                {"label": "South", "meta": "Area", "children": [{"label": "Site 201", "meta": "Site", "children": []}]},
            ]},
        ]
        context["sample_timeline"] = [
            {"day": "Today", "events": [
                {"actor": "Ada", "text": "published Safety inspection", "time": "10:42", "tone": "success", "icon": "send"},
                {"actor": "Sam", "text": "updated section 3", "time": "09:18", "tone": "info", "icon": "pencil"},
            ]},
            {"day": "Yesterday", "events": [
                {"actor": "System", "text": "created version 4", "time": "16:05", "tone": "neutral", "icon": "git-branch"},
            ]},
        ]
        context["sample_tabs"] = [
            {"key": "overview", "label": "Overview", "icon": "info"},
            {"key": "sections", "label": "Sections", "icon": "list"},
            {"key": "activity", "label": "Activity", "icon": "history"},
        ]
        from django.db.models import Count
        from django.urls import reverse

        from apps.locations.selectors import locations_visible_to

        from .ui import STATUSES

        context["anchors"] = [
            ("buttons", "Buttons"), ("cards", "Cards"), ("badges", "Badges"), ("alerts", "Alerts & toasts"),
            ("forms", "Forms"), ("tables", "Tables"), ("overlays", "Modals & menus"), ("navigation", "Tabs & steps"),
            ("tree", "Tree"), ("timeline", "Timeline"), ("states", "States"), ("feedback", "Network & search"),
            ("tokens", "Tokens"),
        ]
        context["statuses"] = list(STATUSES)[:14]
        context["sample_actions"] = [
            {"label": "View", "href": "#tables", "icon": "eye"},
            {"label": "Edit", "href": "#tables", "icon": "pencil"},
            {"label": "Delete", "href": "#tables", "icon": "trash-2", "tone": "danger"},
        ]
        context["sample_steps"] = [
            {"label": "Details", "state": "done"}, {"label": "Sections", "state": "current"}, {"label": "Review", "state": "todo"},
        ]
        context["token_swatches"] = [
            ("primary", "bg-primary text-primary-foreground"), ("secondary", "bg-secondary text-secondary-foreground"),
            ("surface", "bg-surface text-foreground"), ("muted", "bg-muted text-muted-foreground"),
            ("success", "bg-success-soft text-success-text"), ("warning", "bg-warning-soft text-warning-text"),
            ("danger", "bg-danger-soft text-danger-text"), ("info", "bg-info-soft text-info-text"),
        ]
        roots = (
            locations_visible_to(self.request.user)
            .filter(parent__isnull=True)
            .select_related("location_type")
            .annotate(child_total=Count("children"))
            .order_by("name")[:20]
        )
        context["real_tree"] = [
            {
                "label": root.name,
                "meta": f"{root.location_type.name} - {root.code}",
                "href": reverse("locations:detail", args=[root.pk]),
                "status": "active" if root.is_active else "inactive",
                "has_children": root.child_total > 0,
                "children_url": reverse("locations:branch", args=[root.pk]),
                "children": [],
            }
            for root in roots
        ]
        return context

