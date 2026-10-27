"""Rule filters: a short list of comparisons over the event's registered *facts* - an adapter over the Phase 6 operators, not a condition engine.

``[{"attribute", "operator", "value"}, ...]``: every entry must be TRUE for the rule to apply (an implicit AND; no grouping, no nesting - a notification
policy does not need one).  Attributes come from the event's registry entry, operators are the Phase 6 ones valid for the attribute's comparison family,
values are parsed with the same typed operand parser the evaluation engine uses.  A missing or unavailable fact is UNKNOWN, which never applies a rule
(fail closed: nobody is notified by a filter that could not be decided).
"""

from __future__ import annotations

from typing import Any

from apps.checklists.rules.operators import OPERATORS, evaluate_comparison
from apps.checklists.rules.state import ANSWERED, EMPTY, AnswerState
from apps.checklists.rules.tri import Tri
from apps.evaluation.sources import Field, OperandError, parse_field_operand

from . import constants as k
from .registry import Event, Fact


def _field(fact: Fact) -> Field:
    kind = {"int": "decimal", "bool": "bool"}.get(fact.kind, "choice" if fact.family == "choice" else "text")
    return Field(fact.code, fact.label, fact.family, kind, fact.choices)


def clean_filters(event: Event, raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """``(normalized filters, problems)``."""
    if raw in (None, ""):
        return [], []
    if not isinstance(raw, list) or len(raw) > k.MAX_FILTERS:
        return [], [f"filters must be a list of at most {k.MAX_FILTERS} comparisons."]
    facts = {f.code: f for f in event.facts}
    out: list[dict[str, Any]] = []
    problems: list[str] = []
    for i, item in enumerate(raw, 1):
        where = f"filters[{i}]"
        if not isinstance(item, dict) or set(item) - {"attribute", "operator", "value"}:
            problems.append(f"{where}: each filter is {{\"attribute\", \"operator\", \"value\"}}.")
            continue
        fact = facts.get(str(item.get("attribute", "")))
        op = OPERATORS.get(str(item.get("operator", "")).upper())
        if fact is None:
            problems.append(f"{where}: “{item.get('attribute')}” is not a fact of the event “{event.code}” (use: {', '.join(facts) or 'none'}).")
            continue
        if op is None or fact.family not in op.families:
            problems.append(f"{where}: the operator “{item.get('operator')}” does not apply to {fact.label.lower()}.")
            continue
        try:
            stored, _parsed = parse_field_operand(_field(fact), op, item.get("value"))
        except OperandError as exc:
            problems.append(f"{where}: the value {exc}.")
            continue
        entry: dict[str, Any] = {"attribute": fact.code, "operator": op.code}
        if op.operand != "none":
            entry["value"] = stored
        out.append(entry)
    return (out if not problems else []), problems


def matches(event: Event, filters: list[dict[str, Any]], facts: dict[str, Any]) -> bool:
    """True only when every filter is TRUE over ``facts`` (UNKNOWN or a malformed filter = does not apply)."""
    by_code = {f.code: f for f in event.facts}
    for item in filters or []:
        fact, op = by_code.get(str(item.get("attribute", ""))), OPERATORS.get(str(item.get("operator", "")))
        if fact is None or op is None or fact.family not in op.families:
            return False
        try:
            _stored, parsed = parse_field_operand(_field(fact), op, item.get("value"))
        except OperandError:
            return False
        value = facts.get(fact.code)
        state = AnswerState(EMPTY) if value is None else AnswerState(ANSWERED, value, value)
        if evaluate_comparison(op, fact.family, state, parsed) is not Tri.TRUE:
            return False
    return True
