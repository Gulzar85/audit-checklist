"""Report screens (``/reports/``): thin CBVs over ``apps.reporting.services`` (the same code the API uses)."""

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
from django.views.generic import TemplateView

from apps.accounts.context import get_active_organization
from apps.core.policies import is_active_user

from . import constants as k
from . import policies, registry, services
from .errors import ReportConflict, ReportError
from .services import saved as saved_service


def raw_from_query(report: registry.Report, query: QueryDict) -> dict[str, Any]:
    """The request dictionary the services validate: only keys the report registered are read (``f_<filter>``); everything is still validated by the service."""
    filters: dict[str, Any] = {}
    for spec in [*report.filters, registry.FilterSpec("date_from", "date", "From"), registry.FilterSpec("date_to", "date", "To")]:
        value = query.get(f"f_{spec.key}", "")
        if spec.kind == "bool":
            if value in ("1", "on", "true"):
                filters[spec.key] = True
            elif value in ("0", "false"):
                filters[spec.key] = False
        elif value != "":
            filters[spec.key] = value
    return {"filters": filters, "columns": query.getlist("columns"), "sort": query.get("sort", ""), "group_by": query.get("group_by", "")}


def query_of(raw: dict[str, Any]) -> str:
    pairs = [(f"f_{k_}", "1" if v is True else "0" if v is False else v) for k_, v in raw["filters"].items()] + [("columns", c) for c in raw["columns"]]
    if raw["sort"]:
        pairs.append(("sort", raw["sort"]))
    if raw["group_by"]:
        pairs.append(("group_by", raw["group_by"]))
    return urlencode(pairs)


class ReportsMixin(LoginRequiredMixin):
    def organization(self) -> Any:
        org = get_active_organization(self.request)
        if org is None or not policies.can_report(self.request.user, org):
            raise PermissionDenied
        return org


class CatalogueView(ReportsMixin, TemplateView):
    template_name = "reporting/catalogue.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        org = self.organization()
        groups: dict[str, list[registry.Report]] = {}
        for report in policies.visible_reports(self.request.user, org):
            groups.setdefault(report.category, []).append(report)
        return {**super().get_context_data(**kwargs), "organization": org, "groups": groups, "can_export": policies.can_export(self.request.user, org)}


class ReportView(ReportsMixin, View):
    template_name = "reporting/report.html"

    def get(self, request: HttpRequest, code: str) -> HttpResponse:
        org = self.organization()
        report = registry.get(code)
        if report is None:
            raise Http404
        raw = raw_from_query(report, request.GET)
        context: dict[str, Any] = {"report": report, "organization": org, "raw": raw, "query": query_of(raw), "can_export": policies.can_export(request.user, org), "can_share": policies.can_share(request.user, org),
                                   "people": policies.can_see_people(request.user, org), "formats": [(f, dict(k.Format.choices)[f]) for f in report.formats], "result": None, "error": ""}
        try:
            page = int(request.GET.get("page", 1) or 1)
            context["result"] = services.run(request.user, org, code, raw, page=page, page_size=int(request.GET.get("page_size", k.PAGE_SIZE) or k.PAGE_SIZE))
        except ValueError:
            context["error"] = "Page numbers must be whole numbers."
        except ReportError as exc:
            context["error"] = exc.message
        except PermissionDenied:
            raise
        context["spec_values"] = {s.key: raw["filters"].get(s.key, "") for s in report.filters}
        context.update(self.choice_lists(org, report, raw))
        return render(request, self.template_name, context)

    def choice_lists(self, org: Any, report: registry.Report, raw: dict[str, Any]) -> dict[str, Any]:
        from apps.checklists.models import ChecklistTemplate
        from apps.locations.selectors import get_locations_for_organization

        keys = {s.key for s in report.filters}
        out: dict[str, Any] = {"col_keys": set(raw["columns"])}
        if "location" in keys:
            from apps.permissions.authorization import get_accessible_locations
            from apps.permissions.registry import P

            out["locations"] = list(get_accessible_locations(self.request.user, P.REPORT_VIEW, organization=org).order_by("name").values("pk", "name")[:500])
        if "template" in keys:
            out["templates"] = list(ChecklistTemplate.objects.filter(organization=org).order_by("name").values("pk", "name")[:300])
        return out


class ExportView(ReportsMixin, View):
    """POST: request an export of exactly what the page shows (the query string is re-parsed and re-validated server-side)."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, code: str) -> HttpResponse:
        org = self.organization()
        report = registry.get(code)
        if report is None:
            raise Http404
        raw = raw_from_query(report, QueryDict(request.POST.get("query", "")))
        try:
            run = services.request_export(request.user, org, code, raw, request.POST.get("format", ""))
        except ReportError as exc:
            messages.error(request, exc.message)
            return HttpResponseRedirect(f"{reverse('reporting:report', args=[code])}?{query_of(raw)}")
        messages.success(request, "Your export is ready." if run.status == k.RunStatus.SUCCEEDED else "Your export is being prepared; it will appear under Export history when ready.")
        return HttpResponseRedirect(reverse("reporting:run", args=[run.pk]))


class SaveView(ReportsMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, code: str) -> HttpResponse:
        org = self.organization()
        report = registry.get(code)
        if report is None:
            raise Http404
        raw = raw_from_query(report, QueryDict(request.POST.get("query", "")))
        try:
            saved = saved_service.save(request.user, org, code, {**raw, "name": request.POST.get("name", ""), "description": request.POST.get("description", ""),
                                                              "visibility": request.POST.get("visibility", k.Visibility.PRIVATE), "default_format": request.POST.get("default_format", "")})
        except ReportError as exc:
            messages.error(request, exc.message)
            return HttpResponseRedirect(f"{reverse('reporting:report', args=[code])}?{query_of(raw)}")
        messages.success(request, "Saved report created.")
        return HttpResponseRedirect(reverse("reporting:saved"))


class SavedListView(ReportsMixin, TemplateView):
    template_name = "reporting/saved.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        org = self.organization()
        rows = list(saved_service.visible_to(self.request.user, org).order_by("name"))
        for row in rows:
            report = registry.get(row.report_code)
            row.report_title = report.title if report else row.report_code
            row.query = query_of(saved_service.request_of(row))
            row.mine = row.owner_id == self.request.user.pk
        return {**super().get_context_data(**kwargs), "organization": org, "rows": rows}


class SavedActionView(ReportsMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: str, action: str) -> HttpResponse:
        org = self.organization()
        saved = saved_service.get(request.user, org, pk)
        if saved is None:
            raise Http404
        try:
            if action == "duplicate":
                saved_service.duplicate(request.user, org, saved)
                messages.success(request, "A private copy was created.")
            elif action == "delete":
                saved_service.delete(request.user, saved)
                messages.success(request, "Saved report deleted.")
            else:
                raise Http404
        except ReportError as exc:
            messages.error(request, exc.message)
        return HttpResponseRedirect(reverse("reporting:saved"))


class RunListView(ReportsMixin, TemplateView):
    template_name = "reporting/runs.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        org = self.organization()
        runs = list(services.runs_visible_to(self.request.user, org).order_by("-created_at")[:50])
        for run in runs:
            report = registry.get(run.report_code)
            run.report_title = report.title if report else run.report_code
        return {**super().get_context_data(**kwargs), "organization": org, "runs": runs}


class RunDetailView(ReportsMixin, TemplateView):
    template_name = "reporting/run.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        self.organization()
        run = services.get_run(self.request.user, self.kwargs["pk"])
        if run is None:
            raise Http404
        report = registry.get(run.report_code)
        return {**super().get_context_data(**kwargs), "run": run, "report_title": report.title if report else run.report_code,
                "downloadable": run.status == k.RunStatus.SUCCEEDED and policies.can_export(self.request.user, run.organization)}


class DownloadView(ReportsMixin, View):
    """The only way to a file: authenticated, requester-only, re-authorized now, audited; the file name comes from the database, never from the URL."""

    def get(self, request: HttpRequest, pk: str) -> HttpResponse:
        self.organization()
        run = services.get_run(request.user, pk)
        if run is None:
            raise Http404
        try:
            data, mime, filename = services.open_artifact(request.user, run)
        except ReportConflict as exc:
            messages.error(request, exc.message)
            return HttpResponseRedirect(reverse("reporting:run", args=[run.pk]))
        response = HttpResponse(data, content_type=mime)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
