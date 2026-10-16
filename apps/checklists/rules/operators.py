"""The operator registry: which comparisons exist, which answer types allow them, and what they mean.

Operators are *data* with a small pure function - nothing is parsed from user text and nothing is executed.  Which
operators a source item offers is decided by its answer type's ``comparison_family`` (declared in the Phase 5
definition), so a new answer type needs no change here.

Missing-value semantics (documented in ``docs/rule-operators.md``): for every operator except ``IS_EMPTY`` /
``IS_NOT_EMPTY`` a source that is *not answered* (empty, N/A, invalid or hidden) gives ``UNKNOWN`` - never FALSE.
``IS_EMPTY`` is TRUE only for an empty answer, ``IS_NOT_EMPTY`` only for a valid answer; N/A is neither empty nor
answered (both FALSE) and an invalid or hidden source is UNKNOWN for both.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..answer_types import AnswerInvalid, registry
from .limits import limit
from .snapshot import ItemSpec
from .state import ANSWERED, EMPTY, HIDDEN, INVALID, NOT_APPLICABLE, AnswerState
from .tri import Tri

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

EQUALITY, CHOICE, MULTI_CHOICE, ORDERED, TEXT, PRESENCE = "equality", "choice", "multi_choice", "ordered", "text", "presence"
ALL_FAMILIES = frozenset({EQUALITY, CHOICE, MULTI_CHOICE, ORDERED, TEXT, PRESENCE})


def fold(text: str) -> str:
    """Text normalization for comparisons: Unicode NFKC, case-folded, trimmed.  Documented in rule-operators.md."""
    return unicodedata.normalize("NFKC", text).casefold().strip()


class OperandError(Exception):
    """The comparison value is not acceptable for this operator and source item."""


@dataclass(frozen=True)
class OperatorDef:
    code: str
    label: str
    description: str
    families: frozenset[str]
    #: none | scalar | range | option | options | text  (what the comparison value looks like)
    operand: str
    compute: Callable[[Any, Any, str], bool] | None = None

    def describe(self) -> dict[str, Any]:
        return {"code": self.code, "label": self.label, "description": self.description, "operand": self.operand,
                "families": sorted(self.families)}


def _eq(v: Any, o: Any, family: str) -> bool:
    return fold(v) == o if family == TEXT else v == o


def _selected(v: Any, o: Any, family: str) -> bool:
    return o in v if family == MULTI_CHOICE else v == o


def _any(v: Any, o: Any, family: str) -> bool:
    return bool(set(v) & set(o)) if family == MULTI_CHOICE else v in o


_ORDERED = frozenset({ORDERED})
_TEXT = frozenset({TEXT})
_NOT_PRESENCE = ALL_FAMILIES - {PRESENCE}

OPERATORS: dict[str, OperatorDef] = {
    op.code: op
    for op in (
        OperatorDef("EQUALS", "equals", "The answer equals the value.", frozenset({EQUALITY, CHOICE, ORDERED, TEXT}), "scalar", _eq),
        OperatorDef("NOT_EQUALS", "does not equal", "The answer is answered and differs from the value.", frozenset({EQUALITY, CHOICE, ORDERED, TEXT}), "scalar",
                    lambda v, o, f: not _eq(v, o, f)),
        OperatorDef("IS_EMPTY", "is empty", "Nothing has been answered.", ALL_FAMILIES, "none"),
        OperatorDef("IS_NOT_EMPTY", "is answered", "A valid answer has been given.", ALL_FAMILIES, "none"),
        OperatorDef("GREATER_THAN", "is greater than", "Strictly greater (later, for dates).", _ORDERED, "scalar", lambda v, o, f: v > o),
        OperatorDef("GREATER_THAN_OR_EQUAL", "is at least", "Greater than or equal.", _ORDERED, "scalar", lambda v, o, f: v >= o),
        OperatorDef("LESS_THAN", "is less than", "Strictly less (earlier, for dates).", _ORDERED, "scalar", lambda v, o, f: v < o),
        OperatorDef("LESS_THAN_OR_EQUAL", "is at most", "Less than or equal.", _ORDERED, "scalar", lambda v, o, f: v <= o),
        OperatorDef("BETWEEN", "is between", "Within the range, both ends included.", _ORDERED, "range", lambda v, o, f: o[0] <= v <= o[1]),
        OperatorDef("NOT_BETWEEN", "is not between", "Outside the range.", _ORDERED, "range", lambda v, o, f: not (o[0] <= v <= o[1])),
        OperatorDef("CONTAINS", "contains", "The text contains the value (case-insensitive).", _TEXT, "text", lambda v, o, f: o in fold(v)),
        OperatorDef("DOES_NOT_CONTAIN", "does not contain", "The text does not contain the value.", _TEXT, "text", lambda v, o, f: o not in fold(v)),
        OperatorDef("STARTS_WITH", "starts with", "The text starts with the value.", _TEXT, "text", lambda v, o, f: fold(v).startswith(o)),
        OperatorDef("ENDS_WITH", "ends with", "The text ends with the value.", _TEXT, "text", lambda v, o, f: fold(v).endswith(o)),
        OperatorDef("SELECTED", "has selected", "The option is (among) the selected option(s).", frozenset({CHOICE, MULTI_CHOICE}), "option", _selected),
        OperatorDef("NOT_SELECTED", "has not selected", "The option is not selected.", frozenset({CHOICE, MULTI_CHOICE}), "option",
                    lambda v, o, f: not _selected(v, o, f)),
        OperatorDef("CONTAINS_ANY", "has selected any of", "At least one of the options is selected.", frozenset({CHOICE, MULTI_CHOICE}), "options", _any),
        OperatorDef("CONTAINS_ALL", "has selected all of", "Every one of the options is selected.", frozenset({MULTI_CHOICE}), "options",
                    lambda v, o, f: set(o) <= set(v)),
    )
}


def operators_for(answer_type: str) -> list[OperatorDef]:
    """The operators valid for a source item of ``answer_type`` (empty for an unknown type)."""
    if answer_type not in registry:
        return []
    family = registry.get(answer_type).comparison_family
    return [op for op in OPERATORS.values() if family in op.families]


def describe_all() -> dict[str, Any]:
    return {
        "operators": [op.describe() for op in OPERATORS.values()],
        "by_answer_type": {d.code: [op.code for op in operators_for(d.code)] for d in registry.all()},
    }


# ---------------------------------------------------------------------------
# Comparison values (operands)
# ---------------------------------------------------------------------------
def _parse_scalar(item: ItemSpec, raw: Any) -> Any:
    try:
        return item.definition.parse_operand(raw, item.context())
    except AnswerInvalid as exc:
        raise OperandError(exc.message) from None


def _option(item: ItemSpec, raw: Any) -> str:
    if not isinstance(raw, str) or raw not in item.option_values():
        raise OperandError("That option does not exist on the source question.")
    return raw


def clean_operand(op: OperatorDef, item: ItemSpec, raw: Any) -> tuple[Any, Any]:
    """Validate a comparison value with the source item's own answer type.

    Returns ``(stored, parsed)``: ``stored`` is the JSON-safe normalized form saved in the rule (Phase 5 serialization:
    a numeric is a decimal string, a date an ISO string ...), ``parsed`` the Python value the evaluator compares.
    The item's range/length limits are deliberately *not* applied (see ``parse_operand``): "greater than 25" is a valid
    rule for a field that only accepts up to 20.
    """
    definition, family = item.definition, item.definition.comparison_family
    if op.operand == "none":
        if raw not in (None, "", []):
            raise OperandError("This comparison takes no value.")
        return None, None
    if raw is None or raw == "":
        raise OperandError("A comparison value is required.")
    if op.operand == "scalar":
        if family == CHOICE:
            return _option(item, raw), raw
        parsed = _parse_scalar(item, raw)
        if family == TEXT:
            return parsed, fold(parsed)
        return definition.serialize(parsed), parsed
    if op.operand == "range":
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise OperandError("Give a range as two values: [from, to].")
        low, high = _parse_scalar(item, raw[0]), _parse_scalar(item, raw[1])
        if low > high:
            raise OperandError("The start of the range must not be after its end.")
        return [definition.serialize(low), definition.serialize(high)], (low, high)
    if op.operand == "option":
        value = _option(item, raw)
        return value, value
    if op.operand == "options":
        if not isinstance(raw, (list, tuple)) or not raw:
            raise OperandError("Choose at least one option.")
        if len(raw) > limit("max_operand_items"):
            raise OperandError(f"At most {limit('max_operand_items')} options may be listed.")
        values = [_option(item, v) for v in raw]
        if len(set(values)) != len(values):
            raise OperandError("An option is listed twice.")
        return values, tuple(values)
    if op.operand == "text":
        if not isinstance(raw, str) or _CONTROL.search(raw) or not raw.strip():
            raise OperandError("Enter some text to look for.")
        if len(raw.strip()) > limit("max_text_operand_length"):
            raise OperandError(f"The text may be at most {limit('max_text_operand_length')} characters.")
        return raw.strip(), fold(raw)
    raise OperandError("Unsupported comparison value.")  # pragma: no cover - registry bug


# ---------------------------------------------------------------------------
# Evaluation of one comparison
# ---------------------------------------------------------------------------
def evaluate_comparison(op: OperatorDef, family: str, state: AnswerState, operand: Any) -> Tri:
    """Three-valued result of ``op`` on a source answer (see module docstring for the missing-value table)."""
    status = state.status
    if op.code == "IS_EMPTY":
        return Tri.TRUE if status == EMPTY else Tri.FALSE if status in (ANSWERED, NOT_APPLICABLE) else Tri.UNKNOWN
    if op.code == "IS_NOT_EMPTY":
        return Tri.TRUE if status == ANSWERED else Tri.FALSE if status in (EMPTY, NOT_APPLICABLE) else Tri.UNKNOWN
    if status != ANSWERED or op.compute is None:  # EMPTY / N/A / INVALID / HIDDEN
        return Tri.UNKNOWN
    return Tri.of(op.compute(state.value, operand, family))


__all__ = [
    "ALL_FAMILIES", "EMPTY", "HIDDEN", "INVALID", "NOT_APPLICABLE", "OPERATORS", "OperandError", "OperatorDef",
    "clean_operand", "describe_all", "evaluate_comparison", "fold", "operators_for",
]
