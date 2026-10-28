"""Shared machinery for report builders: the execution context, scope enforcement and list-report construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db.models import F, QuerySet
from django.utils import timezone

from apps.locations.selectors import get_descendant_ids
from apps.permissions.authorization import get_accessible_locations
from apps.permissions.registry import P

from .. import constants as k
from .. import registry
from ..filters import Params


@dataclass
class Ctx:
    """Everything a builder may use.  ``organization`` is the one organization this run is for; ``user`` decides scope; ``people`` gates names."""

    user: Any
    organization: Any
    params: Params
    people: bool
    now: datetime

    @property
    def report(self) -> registry.Report:
        return self.params.report

    def locations_for(self, permission: str = P.REPORT_VIEW) -> QuerySet:
        """Locations of the organization where the user holds ``permission`` (organization-wide grants contribute every location, scoped grants their subtree)."""
        return get_accessible_locations(self.user, permission, organization=self.organization)

    def location_filter_ids(self) -> set | None:
        value = self.params.filters.get("location")
        if not value:
            return None
        ids = {value}
        if self.params.filters.get("include_descendants", True):
            ids |= {str(pk) for pk in get_descendant_ids([value])}
        return ids


def apply_common(ctx: Ctx, qs: QuerySet, *, date_field: str = "", location_path: str = "location_id", permission: str = P.REPORT_VIEW) -> QuerySet:
    """Scope first (the user's authorized locations, independent of any filter), then the user's filters.  Returns the narrowed queryset."""
    qs = qs.filter(**{f"{location_path}__in": ctx.locations_for(permission).values("pk")})
    ids = ctx.location_filter_ids()
    if ids is not None:
        qs = qs.filter(**{f"{location_path}__in": ids})
    if date_field:
        qs = qs.filter(**{f"{date_field}__gte": ctx.params.start, f"{date_field}__lt": ctx.params.end})
    for spec in ctx.report.filters:
        value = ctx.params.filters.get(spec.key)
        if value is not None and spec.path:
            qs = qs.filter(**{spec.path: value})
    return qs


def sort_expression(ctx: Ctx, alias_of: dict[str, str]):  # noqa: ANN201
    """The ORDER BY for the requested (already validated) sort; nulls last; ties broken by primary key so paging is deterministic."""
    sort = ctx.params.sort or ctx.report.default_sort
    descending = sort.startswith("-")
    expr = F(alias_of[sort.lstrip("-")])
    return expr.desc(nulls_last=True) if descending else expr.asc(nulls_last=True)


def build_list(ctx: Ctx, qs: QuerySet) -> QuerySet:
    """``values()`` rows keyed ``c_<column>`` for the selected (or default) columns, with a deterministic order."""
    report = ctx.report
    chosen = ctx.params.columns or [c.key for c in report.columns if c.default and (ctx.people or not c.people)]
    sort_key = (ctx.params.sort or report.default_sort).lstrip("-")
    needed = list(dict.fromkeys([*chosen, sort_key]))
    annotations: dict[str, Any] = {}
    for key in needed:
        expr = report.column_map[key].expr
        annotations[f"c_{key}"] = expr(ctx) if callable(expr) else F(expr)
    qs = qs.annotate(**annotations).order_by(sort_expression(ctx, {key: f"c_{key}" for key in needed}), "pk")
    return qs.values("pk", *[f"c_{key}" for key in chosen])


def project(row: dict[str, Any]) -> dict[str, Any]:
    """Drop the ``c_`` prefix and the primary key used for ordering."""
    return {key[2:]: value for key, value in row.items() if key.startswith("c_")}


def columns_for(ctx: Ctx) -> list[registry.Column]:
    report = ctx.report
    if ctx.params.columns:
        return [report.column_map[key] for key in ctx.params.columns]
    return [c for c in report.columns if c.default and (ctx.people or not c.people)]


def duration_minutes(value: Any) -> float | None:
    return None if value is None else round(value.total_seconds() / 60, 1)


def now() -> datetime:
    return timezone.now()
