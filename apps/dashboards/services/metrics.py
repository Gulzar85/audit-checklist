"""Dashboard metrics (Phase 20): the one function the web page, the HTMX widgets and the API all call.

A widget's numbers come from ``apps.reporting.services.execution.run`` - the Phase 19 report it points to - with the dashboard's filter context restricted to the filters
that report supports.  Filters a widget cannot use are listed as ``not_applied`` (never silently widened).  Scope is applied by the report before aggregation.
Values are calculated on demand ("live"); nothing is cached.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from apps.core.utils import parse_uuid
from apps.reporting import constants as rk
from apps.reporting import filters as report_filters
from apps.reporting import registry as report_registry
from apps.reporting.errors import ReportError
from apps.reporting.services import execution

from .. import catalog, kpis, policies

logger = logging.getLogger("apps.dashboards")

CONTEXT_KEYS = ("date_from", "date_to", "location", "include_descendants", "template", "bucket")
BUCKETS = ("day", "week", "month", "quarter")


class DashboardError(Exception):
    def __init__(self, message: str, code: str = "dashboard_error") -> None:
        super().__init__(message)
        self.message, self.code = message, code


def clean_context(raw: Any) -> dict[str, Any]:
    """Validate the shared filter context (date range, location + descendants, checklist template, time bucket).  Unknown keys are errors."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise DashboardError("Filters must be an object.", "invalid_filter")
    unknown = sorted(set(raw) - set(CONTEXT_KEYS))
    if unknown:
        raise DashboardError(f"Unsupported filter(s): {', '.join(map(str, unknown))[:80]}.", "unsupported_filter")
    out: dict[str, Any] = {}
    for key in ("date_from", "date_to"):
        if raw.get(key) not in (None, ""):
            try:
                out[key] = date.fromisoformat(str(raw[key])).isoformat()
            except ValueError as exc:
                raise DashboardError("Dates must be YYYY-MM-DD.", "invalid_filter") from exc
    for key in ("location", "template"):
        if raw.get(key) not in (None, ""):
            parsed = parse_uuid(raw[key]) if isinstance(raw[key], str) else None
            if parsed is None:
                raise DashboardError(f"{key.capitalize()} is not a valid identifier.", "invalid_filter")
            out[key] = str(parsed)
    if raw.get("include_descendants") not in (None, ""):
        if not isinstance(raw["include_descendants"], bool):
            raise DashboardError("include_descendants must be true or false.", "invalid_filter")
        out["include_descendants"] = raw["include_descendants"]
    bucket = raw.get("bucket") or ""
    if bucket and bucket not in BUCKETS:
        raise DashboardError("The time bucket must be day, week, month or quarter.", "invalid_filter")
    if bucket:
        out["bucket"] = bucket
    # the range rules are Phase 19's: validate them once against a report that accepts dates
    try:
        params = report_filters.clean(report_registry.REGISTRY["completion"], {"filters": {k: v for k, v in out.items() if k in ("date_from", "date_to")}}, can_see_people=False)
    except ReportError as exc:
        raise DashboardError(exc.message, exc.code) from exc
    out["_start"], out["_end"] = params.start, params.end
    return out


def period_of(context: dict[str, Any]) -> tuple[date, date]:
    start = timezone.localtime(context["_start"]).date()
    end = timezone.localtime(context["_end"] - timedelta(microseconds=1)).date()
    return start, end


def bucket_for(context: dict[str, Any]) -> str:
    if context.get("bucket"):
        return context["bucket"]
    start, end = period_of(context)
    days = (end - start).days + 1
    return "day" if days <= 31 else "week" if days <= 180 else "month"


def previous_range(context: dict[str, Any]) -> tuple[str, str]:
    """The equivalent period immediately before the selected one (same number of days)."""
    start, end = period_of(context)
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    return (prev_end - timedelta(days=length - 1)).isoformat(), prev_end.isoformat()


def change(current: Any, previous: Any) -> float | None:
    """Percentage change; ``None`` when the baseline is 0 or unavailable (never an infinite or invented percentage)."""
    if current is None or previous in (None, 0):
        return None
    return round((float(current) - float(previous)) / abs(float(previous)) * 100, 1)


def _applied(report: report_registry.Report, context: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    supported = {f.key for f in report.filters} | {"date_from", "date_to"}
    applied = {k: v for k, v in context.items() if not k.startswith("_") and k != "bucket" and k in supported}
    if "include_descendants" in applied and "location" not in applied:
        applied.pop("include_descendants")
    not_applied = sorted(k for k, v in context.items() if not k.startswith("_") and k not in ("bucket", "date_from", "date_to", "include_descendants") and v not in (None, "") and k not in supported)
    return applied, not_applied


def drill_url(report_code: str, context: dict[str, Any], extra: dict[str, Any] | None = None) -> str:
    """A link into the Phase 19 report with only the filters it registers.  The report page re-validates and re-authorizes everything."""
    from apps.reporting.views import query_of

    report = report_registry.get(report_code)
    if report is None:
        return ""
    applied, _ = _applied(report, context)
    names = {f.key for f in report.filters}
    for key, value in (extra or {}).items():
        if key in names and value not in (None, ""):
            applied[key] = value
    return f"{reverse('reporting:report', args=[report_code])}?{query_of({'filters': applied, 'columns': [], 'sort': '', 'group_by': ''})}"


def _rows(user: Any, organization: Any, report_code: str, filters: dict[str, Any], group_by: str) -> list[dict[str, Any]]:
    result = execution.run(user, organization, report_code, {"filters": filters, "group_by": group_by}, page_size=rk.MAX_PAGE_SIZE)
    rows = list(result.rows)
    page = 2
    while len(rows) < result.total and page <= 20:  # summaries are bounded; fetch the remaining groups
        rows += execution.run(user, organization, report_code, {"filters": filters, "group_by": group_by}, page=page, page_size=rk.MAX_PAGE_SIZE).rows
        page += 1
    return rows


def kpi_value(user: Any, organization: Any, kpi: kpis.Kpi, filters: dict[str, Any]) -> dict[str, Any]:
    """``{"value", "status", "note"}`` for one KPI under ``filters`` (already restricted to the report's filters)."""
    rows = _rows(user, organization, kpi.report, {**filters, **kpi.fixed_filters}, kpi.group_by)
    empty = 0 if kpi.empty == "zero" else None
    if kpi.mode == "score_average":
        if not rows:
            return {"value": None, "status": "empty", "note": "No scored executions in scope."}
        if len(rows) > 1:
            return {"value": None, "status": "not_comparable", "note": f"{len(rows)} checklists are in scope; their scores are on different scales. Select one checklist."}
        return {"value": rows[0].get(kpi.metric), "status": "ok", "note": rows[0].get("group", "")}
    if kpi.mode == "top_severity":
        from apps.findings.models import FindingSeverity

        top = FindingSeverity.objects.filter(organization=organization, is_active=True).order_by("-rank").first()
        if top is None:
            return {"value": None, "status": "empty", "note": "No severities are configured."}
        hit = next((r for r in rows if r.get("key") == str(top.pk)), None)
        return {"value": hit.get(kpi.metric, 0) if hit else 0, "status": "ok", "note": top.name, "extra": {"severity": str(top.pk)}}
    if kpi.mode.startswith("outcomes:"):
        wanted = set(kpi.mode.split(":", 1)[1].split(","))
        return {"value": sum(r.get(kpi.metric) or 0 for r in rows if r.get("key") in wanted), "status": "ok", "note": ""}
    value = rows[0].get(kpi.metric, empty) if rows else empty
    return {"value": value, "status": "empty" if value is None else "ok", "note": "No data in this period." if value is None else ""}


def widget_data(user: Any, organization: Any, widget_code: Any, context: dict[str, Any], *, now: Any = None) -> dict[str, Any]:
    """Everything a widget shows, computed now from authorized data.  Raises ``PermissionDenied`` for a widget the user may not use, ``DashboardError`` for an unknown one."""
    widget = catalog.WIDGETS.get(widget_code) if isinstance(widget_code, str) else None
    if widget is None:
        raise DashboardError("That widget does not exist.", "unknown_widget")
    if not policies.can_view(user, organization) or not policies.can_use_widget(user, organization, widget):
        raise PermissionDenied
    report = report_registry.get(policies.widget_report(widget))
    applied, not_applied = _applied(report, context)
    start, end = period_of(context)
    out: dict[str, Any] = {"code": widget.code, "kind": widget.kind, "title": widget.title, "description": widget.description, "not_applied": not_applied,
                           "period": {"from": start.isoformat(), "to": end.isoformat()}, "generated_at": timezone.now().isoformat(), "freshness": "live", "status": "ok"}
    try:
        if widget.kind == catalog.KPI:
            kpi = kpis.get(widget.kpi)
            current = kpi_value(user, organization, kpi, applied)
            out.update(title=kpi.name, description=kpi.description, unit=kpi.unit, value=current["value"], status=current["status"], note=current.get("note", ""), better=kpi.better,
                       definition={"numerator": kpi.numerator, "denominator": kpi.denominator, "version": kpi.version})
            if kpi.metric.endswith("_now"):
                out["previous"] = None
                out["comparison"] = "A current state: there is no previous-period value."
            elif current["status"] == "ok":
                prev_from, prev_to = previous_range(context)
                previous = kpi_value(user, organization, kpi, {**applied, "date_from": prev_from, "date_to": prev_to})
                out["previous"] = previous["value"] if previous["status"] == "ok" else None
                out["change_pct"] = change(current["value"], out["previous"])
                out["comparison"] = f"Compared with {prev_from} to {prev_to}."
            drill_extra = {**kpi.drill_filters, **(current.get("extra") or {})}
            out["drill"] = drill_url(kpi.drill_report, context, drill_extra) if kpi.drill_report else ""
            return out
        group = bucket_for(context) if widget.group_by == "period" else widget.group_by
        rows = _rows(user, organization, widget.report, {**applied, **widget.fixed_filters}, group)
        if group == "severity":  # severities are ordered by the organization's configured rank (most severe first), never by name
            from apps.findings.models import FindingSeverity

            rank = {str(pk): r for pk, r in FindingSeverity.objects.filter(organization=organization).values_list("pk", "rank")}
            rows.sort(key=lambda r: -rank.get(r.get("key"), -1))
        if widget.group_by == "period":
            out["bucket"] = group
        out["metrics"] = [{"key": m.key, "label": m.label, "unit": m.unit} for m in widget.metrics]
        total = len(rows)
        if widget.kind != catalog.TREND:
            rows = rows[: widget.max_items]
        out["truncated"] = total > len(rows)
        out["series"] = [{"label": str(r.get("group") or r.get("location") or ""), "key": r.get("key", ""), "values": {m.key: _num(r.get(m.key)) for m in widget.metrics},
                          "drill": drill_url(widget.drill_report, context, {widget.drill_key: r.get("key")}) if widget.drill_key and widget.drill_report else ""} for r in rows]
        if not out["series"] or all(all(v in (None, 0) for v in s["values"].values()) for s in out["series"]):
            out["status"] = "empty"
        out["drill"] = drill_url(widget.drill_report, context) if widget.drill_report else ""
    except ReportError as exc:
        out.update(status="error", error=exc.message)
    except PermissionDenied:
        raise
    except Exception:  # noqa: BLE001 - a defect: logged (no data), the widget shows a recoverable error
        logger.exception("dashboards.widget_failed", extra={"widget": widget.code})
        out.update(status="error", error="This widget could not be calculated. Try again or narrow the filters.")
    return out


def _num(value: Any) -> Any:
    from decimal import Decimal

    return float(value) if isinstance(value, Decimal) else value
