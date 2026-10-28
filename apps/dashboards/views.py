"""Dashboard screens (``/dashboards/``): CBVs over ``services.metrics`` and ``services.layouts`` (the same code as the API).  Widgets load independently over HTMX, so one
slow or failing widget never blocks the page."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect, QueryDict
from django.shortcuts import render
from django.urls import reverse
from django.views import View

from apps.accounts.context import get_active_organization

from . import catalog, policies
from .services import layouts, metrics


def context_from_query(query: QueryDict) -> dict[str, Any]:
    raw: dict[str, Any] = {key: query.get(key) for key in ("date_from", "date_to", "location", "template", "bucket") if query.get(key)}
    if query.get("include_descendants") in ("1", "on", "true"):
        raw["include_descendants"] = True
    elif query.get("location"):
        raw["include_descendants"] = query.get("include_descendants") not in ("0", "false")
    return raw


def query_of(raw: dict[str, Any]) -> str:
    return urlencode({k: ("1" if v is True else "0" if v is False else v) for k, v in raw.items()})


class DashboardMixin(LoginRequiredMixin):
    def organization(self) -> Any:
        org = get_active_organization(self.request)
        if org is None or not policies.can_view(self.request.user, org):
            raise PermissionDenied
        return org

    def dashboard(self, org: Any) -> catalog.Dashboard:
        dashboard = catalog.DASHBOARDS.get(self.kwargs.get("code", ""))
        if dashboard is None or dashboard not in policies.visible_dashboards(self.request.user, org):
            raise Http404
        return dashboard


class IndexView(DashboardMixin, View):
    def get(self, request: HttpRequest) -> HttpResponse:
        org = self.organization()
        visible = policies.visible_dashboards(request.user, org)
        if not visible:
            return render(request, "dashboards/none.html", {"organization": org})
        return HttpResponseRedirect(reverse("dashboards:dashboard", args=[visible[0].code]))


class DashboardView(DashboardMixin, View):
    template_name = "dashboards/dashboard.html"

    def get(self, request: HttpRequest, code: str) -> HttpResponse:
        org = self.organization()
        dashboard = self.dashboard(org)
        raw = context_from_query(request.GET)
        error, context = "", None
        try:
            context = metrics.clean_context(raw)
        except metrics.DashboardError as exc:
            error = exc.message
        layout = layouts.effective(request.user, org, dashboard)
        widgets = [catalog.WIDGETS[item["code"]] for item in layout["widgets"] if not item["hidden"]]
        from apps.checklists.models import ChecklistTemplate
        from apps.permissions.authorization import get_accessible_locations
        from apps.permissions.registry import P

        period = metrics.period_of(context) if context else None
        return render(request, self.template_name, {
            "organization": org, "dashboard": dashboard, "dashboards": policies.visible_dashboards(request.user, org), "widgets": widgets, "layout_source": layout["source"],
            "raw": raw, "query": query_of(raw), "error": error, "period": period, "bucket": metrics.bucket_for(context) if context else "",
            "locations": list(get_accessible_locations(request.user, P.REPORT_VIEW, organization=org).order_by("name").values("pk", "name")[:500]),
            "templates": list(ChecklistTemplate.objects.filter(organization=org).order_by("name").values("pk", "name")[:300]),
            "can_manage": policies.can_manage(request.user, org)})


class WidgetView(DashboardMixin, View):
    """One widget's HTML (HTMX).  Re-authorizes the dashboard, the widget and every filter on each request."""

    def get(self, request: HttpRequest, code: str, widget: str) -> HttpResponse:
        org = self.organization()
        dashboard = self.dashboard(org)
        if widget not in dashboard.widgets:
            raise Http404
        try:
            context = metrics.clean_context(context_from_query(request.GET))
            data = metrics.widget_data(request.user, org, widget, context)
        except metrics.DashboardError as exc:
            data = {"code": widget, "status": "error", "error": exc.message, "title": catalog.WIDGETS[widget].title}
        return render(request, "dashboards/_widget.html", {"w": data, "spec": catalog.WIDGETS[widget], "dashboard": dashboard, "query": request.GET.urlencode()})


class CustomizeView(DashboardMixin, View):
    template_name = "dashboards/customize.html"

    def items(self, org: Any, dashboard: catalog.Dashboard) -> list[dict[str, Any]]:
        layout = layouts.effective(self.request.user, org, dashboard)
        out = []
        for position, item in enumerate(layout["widgets"], start=1):
            widget = catalog.WIDGETS[item["code"]]
            title = widget.title
            if widget.kind == catalog.KPI:
                from . import kpis

                title = kpis.get(widget.kpi).name
            out.append({"code": item["code"], "hidden": item["hidden"], "title": title, "kind": widget.kind, "position": position})
        return out

    def get(self, request: HttpRequest, code: str) -> HttpResponse:
        org = self.organization()
        dashboard = self.dashboard(org)
        return render(request, self.template_name, {"dashboard": dashboard, "items": self.items(org, dashboard), "can_manage": policies.can_manage(request.user, org),
                                                    "source": layouts.effective(request.user, org, dashboard)["source"]})

    def post(self, request: HttpRequest, code: str) -> HttpResponse:
        org = self.organization()
        dashboard = self.dashboard(org)
        action = request.POST.get("action", "save")
        shared = action in ("save_shared", "reset_shared")
        try:
            if action in ("reset", "reset_shared"):
                layouts.reset(request.user, org, dashboard, shared=shared)
                messages.success(request, "The layout was reset.")
            else:
                codes = request.POST.getlist("widget")
                entries = []
                for code_ in codes:
                    try:
                        position = int(request.POST.get(f"position_{code_}", "0") or 0)
                    except ValueError:
                        position = 0
                    entries.append((position, len(entries), {"code": code_, "hidden": request.POST.get(f"show_{code_}") != "1"}))
                layouts.save(request.user, org, dashboard, [e[2] for e in sorted(entries, key=lambda e: (e[0], e[1]))], shared=shared)
                messages.success(request, "Organization default saved." if shared else "Your layout was saved.")
        except layouts.LayoutError as exc:
            messages.error(request, exc.message)
        return HttpResponseRedirect(reverse("dashboards:dashboard", args=[dashboard.code]))
