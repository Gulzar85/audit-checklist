"""Template tags that let other apps' pages show scoring without importing it (the pages only ``{% load scoring_tags %}``)."""

from __future__ import annotations

from typing import Any

from django import template

from apps.scoring import policies, selectors, services

register = template.Library()


@register.inclusion_tag("scoring/_score_card.html", takes_context=True)
def score_card(context: dict[str, Any], execution: Any) -> dict[str, Any]:
    """The score summary card of an execution - empty when scoring does not apply or the viewer may not see it (never an invented zero)."""
    request = context.get("request")
    user = getattr(request, "user", None)
    empty = {"show": False}
    if user is None or not policies.can_view_score(user, execution):
        return empty
    try:
        view = services.score_of(execution)
    except Exception:  # noqa: BLE001 - a scoring problem must never break the execution page
        return empty
    if not view["applicable"]:
        return empty
    result, outcome = view.get("result"), view.get("outcome")
    if outcome is not None and not policies.can_preview(user, execution):
        outcome = None  # a provisional live score is shown only to those allowed to preview it
    if result is None and outcome is None:
        return {"show": True, "pending": bool(view.get("pending")), "execution": execution, "can_breakdown": False}
    return {"show": True, "result": result, "outcome": outcome, "execution": execution, "can_breakdown": policies.can_view_breakdown(user, execution),
            "current": selectors.current_result(execution) if result is None else result}


@register.simple_tag
def scoring_summary(version: Any, user: Any) -> dict[str, Any]:
    """Facts for a version page: ``{"configured", "enabled", "can_view", "scored", "url"}`` (no queries when the viewer may not read scoring)."""
    if not policies.can_view_config(user, version):
        return {"can_view": False}
    row = selectors.get_config(version)
    return {"can_view": True, "configured": row is not None, "enabled": bool(row and row.enabled),
            "scored": len((row.document or {}).get("items") or {}) if row else 0, "method": row.get_method_display() if row else ""}
