"""Read-side helpers for the scoring screens: the question catalogue of a version and readable summaries of rules.  No business logic."""

from __future__ import annotations

from typing import Any

from . import constants as k
from .config import ConfigError, Issue, ItemRule, _parse_range, dstr, item_maximum, to_decimal


def _label(spec: Any, key: str) -> str:
    if spec.answer_type in ("yes_no", "yes_no_na"):
        return {"yes": "Yes", "no": "No"}.get(key, key)
    if spec.answer_type == "boolean":
        return {"true": "Yes", "false": "No"}.get(key, key)
    for option in spec.options:
        if option.value == key:
            return option.label
    return key


def summarize(spec: Any, rule: dict[str, Any]) -> str:
    """One readable line of a stored rule, e.g. ``Yes → 5 · No → 0 · weight 2``."""
    parts: list[str] = []
    atype = spec.answer_type
    if atype == "score":
        parts.append(f"The answer is the score (out of {spec.config.get('max', '?')})")
    elif atype == "numeric":
        for r in rule.get("ranges") or []:
            lo, hi = r.get("min"), r.get("max")
            left = "[" if r.get("min_inclusive", True) else "("
            right = "]" if r.get("max_inclusive", True) else ")"
            parts.append(f"{left}{lo if lo is not None else '−∞'}, {hi if hi is not None else '∞'}{right} → {r.get('points')}")
    else:
        points = rule.get("points") or {}
        if atype in k.FIXED_POINT_KEYS:
            order = list(k.FIXED_POINT_KEYS[atype])
        elif atype == "boolean":
            order = ["true", "false"]
        else:
            order = [o.value for o in spec.options]  # the question's own answer order (never the storage order of the JSON)
        parts.extend(f"{_label(spec, key)} → {points[key]}" for key in order if key in points)
        if atype == "multiple_choice":
            parts.append("sum of selected" if rule.get("aggregate", "sum") == "sum" else "highest selected")
    weight = rule.get("weight", "1")
    if str(weight) != "1":
        parts.append(f"weight {weight}")
    return " · ".join(parts)


def maximum_of(spec: Any, rule: dict[str, Any]) -> str:
    """The most this question can earn, before its weight (empty when the stored rule cannot be read)."""
    try:
        parsed = ItemRule(spec.code, to_decimal(rule.get("weight", "1")), {key: to_decimal(v) for key, v in (rule.get("points") or {}).items()},
                          str(rule.get("aggregate") or "sum"), tuple(_parse_range(r, spec.code) for r in rule.get("ranges") or []))
    except (ConfigError, TypeError, AttributeError):
        return ""
    return dstr(item_maximum(parsed, spec.answer_type, spec.config)) or "0"


def catalogue(snapshot: Any, document: dict[str, Any], issues: list[Issue]) -> list[dict[str, Any]]:
    """Every question of the version in checklist order, with its scoring state and the issues that concern it."""
    by_subject: dict[str, list[Issue]] = {}
    for issue in issues:
        by_subject.setdefault(issue.subject, []).append(issue)
    items = document.get("items") or {}
    sections = {code: s.name for code, s in snapshot.sections.items()}
    rows = []
    for code, spec in snapshot.items.items():
        rule = items.get(code)
        scorable = spec.answer_type in k.SCORABLE_TYPES
        rows.append({
            "code": code, "question": spec.question, "section": sections.get(spec.section_code, spec.section_code), "section_code": spec.section_code,
            "type": spec.answer_type, "scorable": scorable, "active": spec.active, "scored": rule is not None,
            "summary": summarize(spec, rule) if rule is not None else "", "maximum": maximum_of(spec, rule) if rule is not None else "",
            "weight": rule.get("weight", "1") if rule is not None else "", "issues": by_subject.get(f"item:{code}", []),
        })
    return rows


def sample_fields(snapshot: Any, document: dict[str, Any]) -> list[dict[str, Any]]:
    """The inputs of the what-if form: one per scored question, shaped by its answer type."""
    fields = []
    for code, spec in snapshot.items.items():
        if code not in (document.get("items") or {}):
            continue
        choices: list[tuple[str, str]] = []
        if spec.answer_type in ("yes_no", "yes_no_na"):
            choices = [("yes", "Yes"), ("no", "No")] + ([("na", "N/A")] if spec.answer_type == "yes_no_na" else [])
        elif spec.answer_type == "boolean":
            choices = [("true", "Yes"), ("false", "No")]
        elif spec.answer_type in k.CHOICE_TYPES:
            choices = [(o.value, o.label) for o in spec.options if o.is_active]
        fields.append({"code": code, "question": spec.question, "type": spec.answer_type, "choices": choices,
                       "multi": spec.answer_type == "multiple_choice", "min": spec.config.get("min"), "max": spec.config.get("max")})
    return fields


RANGE_ROWS = 8


def blank_range_rows(rule: dict[str, Any] | None, count: int = RANGE_ROWS) -> list[dict[str, Any]]:
    """Existing ranges followed by empty rows up to ``count`` (the editor posts them all back)."""
    rows = [dict(r) for r in (rule or {}).get("ranges", [])]
    rows += [{} for _ in range(max(count - len(rows), 0))]
    return [{"n": n, **r} for n, r in enumerate(rows)]


def blank_rows(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    return rows + [{} for _ in range(max(count - len(rows), 0))]


__all__ = ["blank_range_rows", "blank_rows", "catalogue", "maximum_of", "sample_fields", "summarize"]
