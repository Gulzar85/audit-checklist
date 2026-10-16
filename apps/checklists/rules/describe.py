"""Human-readable summaries of rules ("If Fire exit accessible equals No, require a comment on ...").

Output is plain text assembled from stored question text and values; templates and API clients must escape it as they
would any user-entered text (the Django templates do, automatically).
"""

from __future__ import annotations

from typing import Any

from .actions import ACTIONS
from .operators import OPERATORS
from .snapshot import ItemSpec, VersionSnapshot

_VERBS = {
    "SHOW_ITEM": "show", "HIDE_ITEM": "hide", "SHOW_SECTION": "show section", "HIDE_SECTION": "hide section",
    "REQUIRE_ITEM": "make required", "MAKE_OPTIONAL": "make optional", "REQUIRE_COMMENT": "require a comment on",
    "REQUIRE_EVIDENCE": "require evidence on", "SET_VALIDATION_CONSTRAINT": "tighten the limits of",
    "OPTION_AVAILABILITY": "restrict the options of", "ADD_INSTRUCTIONS": "add instructions to",
    "DISPLAY_MESSAGE": "display a message",
}


def short(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _label(item: ItemSpec) -> str:
    return short(item.question)


def _value_text(item: ItemSpec, stored: Any) -> str:
    if isinstance(stored, bool):
        return "Yes" if stored else "No"
    if isinstance(stored, list):
        return " – ".join(_value_text(item, v) for v in stored) if len(stored) == 2 and item.definition.comparison_family == "ordered" else ", ".join(_value_text(item, v) for v in stored)
    if isinstance(stored, str):
        for option in item.options:
            if option.value == stored:
                return option.label
        if stored in ("yes", "no", "na") and item.definition.comparison_family == "equality":
            return {"yes": "Yes", "no": "No", "na": "N/A"}[stored]
        unit = item.config.get("unit")
        return f"{stored} {unit}" if unit and item.definition.comparison_family == "ordered" else stored
    return str(stored)


def describe_condition(snapshot: VersionSnapshot, node: dict[str, Any] | None) -> str:
    if not node:
        return "always"
    if "conditions" in node:
        joiner = f" {node['operator'].lower()} "
        parts = [describe_condition(snapshot, child) for child in node["conditions"]]
        text = joiner.join(parts)
        return f"({text})" if len(parts) > 1 and node is not None else text
    item = snapshot.items.get(node.get("source_item_code", ""))
    if item is None:
        return f"{node.get('source_item_code')} (unknown question)"
    op = OPERATORS[node["comparison"]]
    value = "" if op.operand == "none" else " " + _value_text(item, node.get("value"))
    return f"“{_label(item)}” {op.label}{value}"


def describe_action(snapshot: VersionSnapshot, action: dict[str, Any]) -> str:
    verb = _VERBS.get(action["type"], action["type"].lower())
    target = action.get("target")
    if action["type"] == "DISPLAY_MESSAGE":
        return f"display a {action['severity']} message: “{short(action['message'], 80)}”"
    if not target:
        return verb
    if target["type"] == "item" and target["code"] in snapshot.items:
        name = f"“{_label(snapshot.items[target['code']])}”"
    elif target["type"] == "section" and target["code"] in snapshot.sections:
        name = f"“{short(snapshot.sections[target['code']].name)}”"
    else:
        name = target["code"]
    extra = ""
    if action["type"] == "OPTION_AVAILABILITY":
        extra = f" ({action['mode']}: {', '.join(action['options'])})"
    elif action["type"] == "SET_VALIDATION_CONSTRAINT":
        extra = " (" + ", ".join(f"{k} {v}" for k, v in action["constraints"].items()) + ")"
    elif action["type"] == "REQUIRE_EVIDENCE":
        extra = f" ({', '.join(action['kinds'])})"
    return f"{verb} {name}{extra}"


def describe_rule(snapshot: VersionSnapshot, condition: dict[str, Any] | None, actions: list[dict[str, Any]]) -> str:
    """``If <condition>, <action>; <action>.``  (``Always, ...`` for an unconditional rule.)"""
    lead = "Always" if not condition else "If " + describe_condition(snapshot, condition)
    effects = "; ".join(describe_action(snapshot, a) for a in actions if isinstance(a, dict) and a.get("type") in ACTIONS)
    return f"{lead}, {effects}." if effects else f"{lead}."
