"""Template helpers for django-tables2 rendering.

django-tables2 exposes configurable prefixes (``table.prefixed_order_by_field``,
``table.prefixed_page_field``) as *values*, so they cannot be used as template
keyword names.  ``querystring_replace`` takes them positionally and rewrites the
current querystring, preserving every other parameter.
"""

from __future__ import annotations

from typing import Any

from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def querystring_replace(context: dict[str, Any], key: str, value: Any = None) -> str:
    """Return the current query string with ``key`` set to ``value``.

    Pass ``value`` as ``None``/empty to remove ``key`` (e.g. resetting to page 1).
    """
    request = context.get("request")
    if request is None:
        return ""
    query = request.GET.copy()
    if value in (None, ""):
        query.pop(key, None)
    else:
        query[key] = value
    return query.urlencode()


@register.filter
def table_page_range(page: Any, paginator: Any) -> list[Any]:
    """Return an elided page list (numbers plus ``"..."``) for a paginator."""
    try:
        return [
            "..." if item == paginator.ELLIPSIS else item
            for item in paginator.get_elided_page_range(page.number)
        ]
    except Exception:  # pragma: no cover - defensive
        return []
