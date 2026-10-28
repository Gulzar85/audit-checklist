"""Small template helpers for dashboards."""

from __future__ import annotations

from typing import Any

from django import template

register = template.Library()


@register.filter
def dash_value(values: dict[str, Any], key: str) -> Any:
    return values.get(key) if hasattr(values, "get") else None


@register.filter
def make_list_words(text: str) -> list[str]:
    return str(text).split()
