"""Display helpers for report tables."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django import template

from apps.reporting.services import export

register = template.Library()


@register.filter
def lookup(mapping: Any, key: Any) -> Any:
    return mapping.get(key, "") if hasattr(mapping, "get") else ""


@register.filter
def cell(row: dict[str, Any], column: Any) -> str:
    """Display text of one cell (auto-escaped by the template engine; formula neutralization is an export concern)."""
    return export.text_of(row.get(column.key), column.kind)


@register.filter
def date_before(end: Any) -> str:
    """The last included day of a half-open range."""
    return (end - timedelta(seconds=1)).strftime("%b %d, %Y") if end else ""
