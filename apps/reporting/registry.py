"""The controlled report registry (Phase 19).

A report is a *server-side definition*: stable code and version, the columns (each mapped to an allowlisted ORM expression), the filters it supports (each mapped
to an allowlisted lookup), the grouping options, the default sort, limits, formats and the permission it needs.  Nothing a user submits is ever used as a field
name, a lookup, an expression or SQL: user input only *selects* among the keys registered here, and is validated against them.

Two kinds: ``list`` (one row per record, ``values()`` rows built by the database) and ``summary`` (grouped metrics computed with database aggregation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from django.core.exceptions import ImproperlyConfigured

from . import constants as k

TEXT, INT, DECIMAL, DATE, DATETIME, BOOL = "text", "int", "decimal", "date", "datetime", "bool"


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: str = TEXT
    expr: Any = None  # an ORM expression / path (list reports); ``None`` for summary columns
    people: bool = False  # shows who: needs ``report.people``
    sortable: bool = True
    default: bool = True  # shown unless the user selects columns


@dataclass(frozen=True)
class FilterSpec:
    key: str
    kind: str  # "date" | "uuid" | "choice" | "bool"
    label: str
    path: str = ""  # the allowlisted ORM lookup (e.g. "version__template_id"); empty for date / location / handled filters
    choices: tuple[tuple[str, str], ...] = ()
    people: bool = False


@dataclass(frozen=True)
class Report:
    code: str
    version: int
    title: str
    description: str
    category: str
    kind: str  # "list" | "summary"
    columns: tuple[Column, ...]
    filters: tuple[FilterSpec, ...]
    build: Callable[..., Any]  # build(ctx) -> QuerySet of dict rows (list) | list[dict] (summary)
    date_field: str = ""  # the lookup the date range applies to (list reports)
    date_label: str = ""  # what the date means, shown to users
    location_path: str = "location_id"  # the lookup of the record's location (scope + location filter)
    group_by: tuple[tuple[str, str], ...] = ()
    default_sort: str = "-created"
    max_rows: int = k.MAX_ROWS
    formats: tuple[str, ...] = (k.Format.CSV, k.Format.XLSX, k.Format.PDF)
    source_permission: str = ""  # shown in the catalogue only to users who hold it somewhere
    notes: str = ""
    enabled: bool = True
    column_map: dict[str, Column] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "column_map", {c.key: c for c in self.columns})

    def filter_spec(self, key: str) -> FilterSpec | None:
        return next((f for f in self.filters if f.key == key), None)


REGISTRY: dict[str, Report] = {}


def register(report: Report) -> Report:
    if report.code in REGISTRY:
        raise ImproperlyConfigured(f"report {report.code!r} is already registered")
    keys = [c.key for c in report.columns]
    if len(keys) != len(set(keys)):
        raise ImproperlyConfigured(f"report {report.code!r} has duplicate columns")
    if report.default_sort.lstrip("-") not in report.column_map:
        raise ImproperlyConfigured(f"report {report.code!r}: unknown default sort")
    if report.kind not in ("list", "summary") or not set(report.formats) <= set(k.Format.values):
        raise ImproperlyConfigured(f"report {report.code!r}: invalid kind or formats")
    REGISTRY[report.code] = report
    return report


def get(code: Any) -> Report | None:
    report = REGISTRY.get(code) if isinstance(code, str) else None
    return report if report is not None and report.enabled else None


def describe(report: Report, *, people: bool = True) -> dict[str, Any]:
    """The public description of a report (no expressions, no lookups)."""
    return {"code": report.code, "version": report.version, "title": report.title, "description": report.description, "category": report.category, "kind": report.kind,
            "columns": [{"key": c.key, "label": c.label, "type": c.kind, "sortable": c.sortable, "default": c.default} for c in report.columns if people or not c.people],
            "filters": [{"key": f.key, "label": f.label, "type": f.kind, "choices": [{"value": v, "label": l} for v, l in f.choices]} for f in report.filters if people or not f.people],
            "group_by": [{"value": v, "label": l} for v, l in report.group_by], "default_sort": report.default_sort, "formats": list(report.formats), "max_rows": report.max_rows,
            "date_filter_applies_to": report.date_label, "notes": report.notes}
