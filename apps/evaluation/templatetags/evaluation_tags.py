"""Template tags that let other apps' pages show evaluation without importing it (the pages only ``{% load evaluation_tags %}``)."""

from __future__ import annotations

from typing import Any

from django import template

from apps.evaluation import constants as k
from apps.evaluation import policies, selectors, services

register = template.Library()


@register.inclusion_tag("evaluation/_eval_card.html", takes_context=True)
def evaluation_card(context: dict[str, Any], execution: Any) -> dict[str, Any]:
    """The evaluation summary card of an execution - empty when evaluation does not apply or the viewer may not see it (never an invented PASS)."""
    request = context.get("request")
    user = getattr(request, "user", None)
    empty = {"show": False}
    if user is None or not policies.can_view_result(user, execution):
        return empty
    try:
        view = services.evaluation_of(execution)
    except Exception:  # noqa: BLE001 - an evaluation problem must never break the execution page
        return empty
    if not view["applicable"]:
        return empty
    run, evaluation = view.get("run"), view.get("evaluation")
    if evaluation is not None and not policies.can_preview(user, execution):
        evaluation = None  # a provisional live evaluation is shown only to those allowed to preview it
    return {"show": True, "execution": execution, "run": run, "evaluation": evaluation.as_dict() if evaluation is not None else None, "pending": bool(view.get("pending")),
            "can_explain": policies.can_explain(user, execution), "outcome_label": dict(k.Outcome.choices).get(run.outcome if run else (evaluation.outcome if evaluation else ""), ""),
            "action_count": selectors.actions_for(run).count() if run is not None and policies.can_explain(user, execution) else None}


@register.simple_tag
def evaluation_summary(version: Any, user: Any) -> dict[str, Any]:
    """Facts for a version page: ``{"can_view", "configured", "enabled", "rules"}`` (no queries when the viewer may not read the rules)."""
    if not policies.can_view_config(user, version):
        return {"can_view": False}
    row = selectors.get_ruleset(version)
    return {"can_view": True, "configured": row is not None, "enabled": bool(row and row.enabled), "rules": len((row.document or {}).get("rules") or []) if row else 0}


@register.filter
def outcome_tone(outcome: str) -> str:
    return {"pass": "success", "warning": "warning", "review_required": "info", "fail": "danger", "indeterminate": "neutral", "not_applicable": "neutral"}.get(str(outcome), "neutral")


@register.filter
def outcome_icon(outcome: str) -> str:
    return {"pass": "circle-check", "warning": "triangle-alert", "review_required": "eye", "fail": "octagon-alert", "indeterminate": "help-circle", "not_applicable": "minus-circle"}.get(str(outcome), "info")


@register.filter
def outcome_text(outcome: str) -> str:
    return dict(k.Outcome.choices).get(str(outcome), str(outcome))
