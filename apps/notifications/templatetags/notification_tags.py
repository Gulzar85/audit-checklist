"""The topbar bell: the unread count and the most recent notifications of the signed-in user (two indexed queries; nothing for an anonymous visitor)."""

from __future__ import annotations

from typing import Any

from django import template

from apps.notifications import selectors

register = template.Library()


@register.inclusion_tag("notifications/_bell.html", takes_context=True)
def notification_bell(context: dict[str, Any]) -> dict[str, Any]:
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return {"items": [], "unread": 0, "show": False}
    try:
        items = list(selectors.inbox(user)[:5])
        return {"items": items, "unread": selectors.unread_count(user), "show": True}
    except Exception:  # noqa: BLE001 - the bell must never break a page
        return {"items": [], "unread": 0, "show": False}
