"""The fixed **input-source registry** of the evaluation engine.

A condition may read only what is registered here - there is no model-field traversal, no ORM lookup and no expression:

* ``answer`` - a question of the rule set's checklist version.  The field is the question's code; the operators, the comparison value and the
  comparison family come from the question's Phase 5 answer type through the Phase 6 operator registry (``clean_operand``), and the value is the
  Phase 6 *answer state* of the server-side evaluation (answered / empty / N/A / invalid / hidden).
* ``score`` - the authoritative Phase 15 result (never recalculated here).  Values are available only when a **final** score exists; a
  provisional, not-applicable, absent or disabled score makes the numeric fields *unavailable* (the condition is UNKNOWN - never 0) and the
  ``status`` field says why.
* ``execution`` - approved execution metadata.

A fact that is unavailable is an ``INVALID`` answer state, which the Phase 6 operators turn into UNKNOWN for every operator (``IS_EMPTY`` included).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from apps.checklists.rules.operators import CHOICE, EQUALITY, ORDERED, TEXT, fold
from apps.checklists.rules.state import ANSWERED, EMPTY, INVALID, AnswerState

SCORE_STATUSES = ("final", "provisional", "not_applicable", "unavailable")
EXECUTION_STATUSES = ("in_progress", "submitted", "cancelled")


@dataclass(frozen=True)
class Field:
    code: str
    label: str
    family: str
    kind: str  # decimal | text | bool | choice
    choices: tuple[str, ...] | None = None


FIELDS: dict[str, dict[str, Field]] = {
    "score": {f.code: f for f in (
        Field("percentage", "Score percentage", ORDERED, "decimal"),
        Field("earned", "Points earned", ORDERED, "decimal"),
        Field("available", "Points available", ORDERED, "decimal"),
        Field("grade", "Grade", TEXT, "text"),
        Field("critical", "Critical failure flagged", EQUALITY, "bool"),
        Field("status", "Score status", CHOICE, "choice", SCORE_STATUSES),
    )},
    "execution": {f.code: f for f in (
        Field("status", "Execution status", CHOICE, "choice", EXECUTION_STATUSES),
    )},
}


class OperandError(Exception):
    pass


def _decimal(raw: Any) -> Decimal:
    if isinstance(raw, (bool, float)) or raw is None:
        raise OperandError("must be a number written as text or a whole number")
    try:
        out = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise OperandError("must be a number") from None
    if not out.is_finite() or abs(out) > Decimal("1e12"):
        raise OperandError("must be a finite number")
    return out


def _dtext(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def _scalar(field: Field, raw: Any) -> tuple[Any, Any]:
    """``(stored, parsed)`` of one comparison value for a registered field."""
    if field.kind == "decimal":
        value = _decimal(raw)
        return _dtext(value), value
    if field.kind == "bool":
        if not isinstance(raw, bool):
            raise OperandError("must be true or false")
        return raw, raw
    if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 100 or any(unicodedata.category(c) == "Cc" for c in raw):
        raise OperandError("must be text of 1 to 100 characters")
    text = raw.strip()
    if field.kind == "choice":
        if field.choices is not None and text not in field.choices:
            raise OperandError("must be one of: " + ", ".join(field.choices))
        return text, text
    return text, fold(text)  # text family: compared case-folded


def parse_field_operand(field: Field, op: Any, raw: Any) -> tuple[Any, Any]:
    """Validate the comparison value of ``op`` for ``field``.  ``OperandError`` carries a readable reason."""
    kind = op.operand
    if kind == "none":
        if raw not in (None, "", []):
            raise OperandError("takes no value")
        return None, None
    if kind in ("scalar", "option", "text"):
        return _scalar(field, raw)
    if kind == "range":
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise OperandError("needs two values")
        (s1, p1), (s2, p2) = _scalar(field, raw[0]), _scalar(field, raw[1])
        if p1 > p2:
            raise OperandError("the first value must not exceed the second")
        return [s1, s2], (p1, p2)
    if kind == "options":
        if not isinstance(raw, (list, tuple)) or not raw or len(raw) > 20 or len({str(v) for v in raw}) != len(raw):
            raise OperandError("needs a list of 1 to 20 distinct values")
        pairs = [_scalar(field, v) for v in raw]
        return [s for s, _p in pairs], tuple(p for _s, p in pairs)
    raise OperandError("unsupported value")


# ---------------------------------------------------------------------------------------------------------------------------------
# The states a run reads
# ---------------------------------------------------------------------------------------------------------------------------------
def _answered(value: Any) -> AnswerState:
    return AnswerState(ANSWERED, value, value)


_UNAVAILABLE = AnswerState(INVALID)


def score_states(result: Any | None, *, reason: str = "unavailable") -> dict[str, AnswerState]:
    """The ``score`` facts: from the stored current ``ScoreResult`` (final only), else unavailable with ``status`` explaining why."""
    if result is None:
        return {"status": _answered(reason if reason in SCORE_STATUSES else "unavailable"), **{c: _UNAVAILABLE for c in ("percentage", "earned", "available", "grade", "critical")}}
    status = str(result.status)
    states = {"status": _answered(status)}
    if status != "final":
        return {**states, **{c: _UNAVAILABLE for c in ("percentage", "earned", "available", "grade", "critical")}}
    states["percentage"] = _answered(result.percentage) if result.percentage is not None else _UNAVAILABLE
    states["earned"] = _answered(result.earned) if result.earned is not None else _UNAVAILABLE
    states["available"] = _answered(result.available) if result.available is not None else _UNAVAILABLE
    states["grade"] = _answered(result.grade) if result.grade else AnswerState(EMPTY)
    states["critical"] = _answered(bool(result.critical))
    return states


def synthetic_score_states(data: dict[str, Any]) -> dict[str, AnswerState]:
    """Preview: score facts supplied by the designer (``status`` default ``final``).  Mirrors ``score_states`` without a stored result."""
    status = str(data.get("status") or "final")
    if status not in SCORE_STATUSES:
        raise OperandError(f"score status must be one of: {', '.join(SCORE_STATUSES)}")
    out: dict[str, AnswerState] = {"status": _answered(status)}
    for code in ("percentage", "earned", "available", "grade", "critical"):
        raw = data.get(code)
        if status != "final" or raw in (None, ""):
            out[code] = _UNAVAILABLE
        elif code in ("percentage", "earned", "available"):
            out[code] = _answered(_decimal(raw))
        elif code == "critical":
            out[code] = _answered(bool(raw) if isinstance(raw, bool) else str(raw).lower() in ("true", "1", "yes", "on"))
        else:
            out[code] = _answered(str(raw))
    return out


def execution_states(execution: Any) -> dict[str, AnswerState]:
    return {"status": _answered(str(execution.status))}


def describe_fields() -> dict[str, Any]:
    return {source: [{"code": f.code, "label": f.label, "family": f.family, "kind": f.kind, "choices": list(f.choices) if f.choices else None}
                     for f in fields.values()] for source, fields in FIELDS.items()}
