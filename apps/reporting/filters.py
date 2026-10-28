"""Validation of a report request into a ``Params`` object (typed, normalized, bounded).

Every key a user sends is checked against the report definition: unsupported filters, columns, sort and grouping are *errors*, not ignored.  Scope is **not** a filter:
the executor intersects the user's authorized rows with whatever the filters select, so no filter value can widen access.

Dates: ``date_from`` and ``date_to`` are calendar dates in the server time zone (``settings.TIME_ZONE``); both are **inclusive**.  Internally the range is the half-open
interval ``[date_from 00:00, (date_to + 1 day) 00:00)``.  No date means the last ``DEFAULT_RANGE_DAYS`` days; the range may not exceed ``MAX_RANGE_DAYS``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from django.utils import timezone

from apps.core.utils import parse_uuid

from . import constants as k
from . import registry
from .errors import ReportError

ALLOWED_TOP_LEVEL = {"filters", "columns", "sort", "group_by"}


@dataclass
class Params:
    report: registry.Report
    filters: dict[str, Any] = field(default_factory=dict)  # normalized, JSON-safe
    columns: list[str] = field(default_factory=list)
    sort: str = ""
    group_by: str = ""
    start: datetime | None = None  # inclusive
    end: datetime | None = None  # exclusive

    def normalized(self) -> dict[str, Any]:
        return {"filters": dict(sorted(self.filters.items())), "columns": list(self.columns), "sort": self.sort, "group_by": self.group_by}


def _parse_date(value: Any, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ReportError(f"{label} must be a date (YYYY-MM-DD).", "invalid_filter") from exc


def day_start(day: date) -> datetime:
    return timezone.make_aware(datetime(day.year, day.month, day.day), timezone.get_default_timezone())


def clean(report: registry.Report, raw: Any, *, can_see_people: bool, now: datetime | None = None) -> Params:
    """Validate ``raw`` (``{"filters": {...}, "columns": [...], "sort": "-key", "group_by": "key"}``) for ``report``."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ReportError("The request must be an object.", "invalid_request")
    extra = sorted(set(raw) - ALLOWED_TOP_LEVEL)
    if extra:
        raise ReportError(f"Unsupported option(s): {', '.join(map(str, extra))[:80]}.", "invalid_request")
    now = now or timezone.now()
    flt = raw.get("filters") or {}
    if not isinstance(flt, dict):
        raise ReportError("filters must be an object.", "invalid_filter")
    supported = {f.key: f for f in report.filters if can_see_people or not f.people}
    supported.update({"date_from": registry.FilterSpec("date_from", "date", "From"), "date_to": registry.FilterSpec("date_to", "date", "To")} if report.date_field or report.kind == "summary" else {})
    unknown = sorted(set(flt) - set(supported))
    if unknown:
        raise ReportError(f"This report does not support the filter(s): {', '.join(map(str, unknown))[:80]}.", "unsupported_filter")
    normalized: dict[str, Any] = {}
    for key, value in flt.items():
        spec = supported[key]
        if value in (None, ""):
            continue
        if spec.kind == "date":
            normalized[key] = _parse_date(value, spec.label).isoformat()
        elif spec.kind == "uuid":
            parsed = parse_uuid(value) if isinstance(value, str) else None
            if parsed is None:
                raise ReportError(f"{spec.label} is not a valid identifier.", "invalid_filter")
            normalized[key] = str(parsed)
        elif spec.kind == "choice":
            if value not in {v for v, _ in spec.choices}:
                raise ReportError(f"{spec.label} must be one of: {', '.join(v for v, _ in spec.choices)}.", "invalid_filter")
            normalized[key] = value
        elif spec.kind == "bool":
            if not isinstance(value, bool):
                raise ReportError(f"{spec.label} must be true or false.", "invalid_filter")
            normalized[key] = value
    today = timezone.localdate(now)
    end_day = date.fromisoformat(normalized["date_to"]) if "date_to" in normalized else today
    start_day = date.fromisoformat(normalized["date_from"]) if "date_from" in normalized else end_day - timedelta(days=k.DEFAULT_RANGE_DAYS - 1)
    if end_day < start_day:
        raise ReportError("The end date is before the start date.", "invalid_range")
    if (end_day - start_day).days + 1 > k.MAX_RANGE_DAYS:
        raise ReportError(f"A report may cover at most {k.MAX_RANGE_DAYS} days.", "range_too_long")
    columns = raw.get("columns") or []
    if not isinstance(columns, list) or len(columns) > len(report.columns) or len(set(map(str, columns))) != len(columns):
        raise ReportError("columns must be a list of distinct column keys.", "invalid_columns")
    allowed = {c.key for c in report.columns if can_see_people or not c.people}
    bad = [c for c in columns if not isinstance(c, str) or c not in allowed]
    if bad:
        raise ReportError(f"Unsupported column(s): {', '.join(map(str, bad))[:80]}.", "invalid_columns")
    sort = raw.get("sort") or ""
    if sort:
        key = sort[1:] if isinstance(sort, str) and sort.startswith("-") else sort
        column = report.column_map.get(key) if isinstance(key, str) else None
        if column is None or not column.sortable or (column.people and not can_see_people):
            raise ReportError("That sort is not supported.", "invalid_sort")
    group = raw.get("group_by") or ""
    if group and group not in {v for v, _ in report.group_by}:
        raise ReportError("That grouping is not supported.", "invalid_group")
    return Params(report=report, filters=normalized, columns=list(columns), sort=sort, group_by=group, start=day_start(start_day), end=day_start(end_day + timedelta(days=1)))
