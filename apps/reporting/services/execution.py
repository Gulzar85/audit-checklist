"""Report execution (Phase 19): the one place that validates, authorizes and runs a report.  Web, API, exports, saved reports and Phase 18 jobs all call this."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.utils import timezone

from .. import constants as k
from .. import filters, policies, registry
from ..errors import ReportError
from ..reports import _base
from ..reports import definitions  # noqa: F401  (registers the reports)


@dataclass
class Result:
    report: registry.Report
    params: filters.Params
    columns: list[registry.Column]
    rows: list[dict[str, Any]]
    total: int
    page: int
    page_size: int
    generated_at: datetime
    organization: Any = None
    truncated: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size)) if self.page_size else 1


def prepare(user: Any, organization: Any, code: Any, raw: Any, *, now: datetime | None = None) -> tuple[_base.Ctx, registry.Report]:
    """Validate the request and authorize it: the report must exist and be enabled, the user must hold ``report.view`` in the organization (and the report's source permission),
    and every filter / column / sort / grouping must be one the report registered.  Raises ``PermissionDenied`` / ``ReportError``."""
    from django.core.exceptions import PermissionDenied

    report = registry.get(code)
    if report is None:
        raise ReportError("That report is not available.", "unknown_report")
    if not policies.can_use_report(user, organization, report):
        raise PermissionDenied
    now = now or timezone.now()
    people = policies.can_see_people(user, organization)
    params = filters.clean(report, raw, can_see_people=people, now=now)
    return _base.Ctx(user=user, organization=organization, params=params, people=people, now=now), report


def _columns(ctx: _base.Ctx) -> list[registry.Column]:
    if ctx.report.kind == "summary":
        return [c for c in ctx.report.columns if ctx.people or not c.people]
    return _base.columns_for(ctx)


def _sorted(rows: list[dict[str, Any]], ctx: _base.Ctx) -> list[dict[str, Any]]:
    sort = ctx.params.sort or ctx.report.default_sort
    key, descending = sort.lstrip("-"), sort.startswith("-")
    present = [r for r in rows if r.get(key) is not None]
    missing = [r for r in rows if r.get(key) is None]
    present.sort(key=lambda r: (str(r[key]).lower() if isinstance(r[key], str) else r[key]), reverse=descending)
    return present + missing


def run(user: Any, organization: Any, code: Any, raw: Any = None, *, page: int = 1, page_size: int = k.PAGE_SIZE, now: datetime | None = None) -> Result:
    """One page of results.  Reads only; the result set is bounded (``max_rows``)."""
    ctx, report = prepare(user, organization, code, raw, now=now)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), k.MAX_PAGE_SIZE))
    columns = _columns(ctx)
    if report.kind == "summary":
        rows = _sorted(report.build(ctx), ctx)
        total = len(rows)
        window = rows[(page - 1) * page_size: page * page_size]
    else:
        qs = report.build(ctx)
        total = qs.count()
        offset = (page - 1) * page_size
        window = [_base.project(r) for r in qs[offset: offset + page_size] if offset < report.max_rows]
    return Result(report, ctx.params, columns, window, min(total, report.max_rows), page, page_size, ctx.now, organization, truncated=total > report.max_rows)


def all_rows(user: Any, organization: Any, code: Any, raw: Any = None, *, now: datetime | None = None) -> Result:
    """Every row up to ``max_rows`` (for exports).  Raises ``too_large`` rather than silently truncating."""
    ctx, report = prepare(user, organization, code, raw, now=now)
    columns = _columns(ctx)
    if report.kind == "summary":
        rows = _sorted(report.build(ctx), ctx)
    else:
        qs = report.build(ctx)
        rows = [_base.project(r) for r in qs[: report.max_rows + 1].iterator(chunk_size=500)]
    if len(rows) > report.max_rows:
        raise ReportError(f"The report has more than {report.max_rows} rows. Narrow the filters.", "too_large")
    return Result(report, ctx.params, columns, rows, len(rows), 1, max(1, len(rows)), ctx.now, organization)


def count(user: Any, organization: Any, code: Any, raw: Any = None) -> int:
    """How many rows a request would produce (used to decide between a synchronous and a background export)."""
    ctx, report = prepare(user, organization, code, raw)
    return len(report.build(ctx)) if report.kind == "summary" else min(report.build(ctx).count(), report.max_rows + 1)
