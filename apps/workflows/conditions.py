"""Workflow conditions - an adapter over the **Phase 6 rules engine**, not a second evaluator (ADR 0070).

A condition is a small JSON tree: a *leaf* ``{"attribute", "operator", "value"}`` or a *group* ``{"logic": "AND"|"OR", "conditions": [...]}``.
Attributes come from the target registry (``targets.py``); operators, their per-family applicability, missing-value semantics and the
three-valued logic are the Phase 6 ones (``apps.checklists.rules.operators`` / ``tri``).  Nothing is parsed from text and nothing is executed.

Failing closed: a missing value, an unknown attribute or a malformed node evaluates to ``UNKNOWN``, which never selects a transition and
never lets a record start - exactly as ``UNKNOWN`` never triggers a Phase 6 rule action.
"""

from __future__ import annotations

from decimal import Decimal  # noqa: F401
from typing import Any

from apps.checklists.rules.operators import OPERATORS, evaluate_comparison
from apps.checklists.rules.state import ANSWERED, EMPTY, AnswerState
from apps.checklists.rules.tri import Tri, tri_and, tri_or

from .targets import Attribute, get_spec

MAX_DEPTH, MAX_LEAVES, MAX_OPTIONS = 3, 20, 20


def _scalar(attr: Attribute, raw: Any) -> Any:
    if attr.kind == "bool":
        if not isinstance(raw, bool):
            raise ValueError("must be true or false")
        return raw
    if attr.kind == "int":
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError("must be a whole number")
        return raw
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 100:
        raise ValueError("must be text of at most 100 characters")
    value = raw.strip()
    if attr.choices is not None and value not in attr.choices:
        raise ValueError("must be one of: " + ", ".join(attr.choices))
    return value


def _operand(attr: Attribute, op, raw: Any) -> Any:  # noqa: ANN001
    kind = op.operand
    if kind == "none":
        if raw not in (None, ""):
            raise ValueError("takes no value")
        return None
    if kind in ("scalar", "option", "text"):
        return _scalar(attr, raw)
    if kind == "range":
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ValueError("needs two values")
        low, high = _scalar(attr, raw[0]), _scalar(attr, raw[1])
        if low > high:
            raise ValueError("the first value must not exceed the second")
        return [low, high]
    if kind == "options":
        if not isinstance(raw, (list, tuple)) or not raw or len(raw) > MAX_OPTIONS or len(set(map(str, raw))) != len(raw):
            raise ValueError("needs a list of 1 to %d distinct values" % MAX_OPTIONS)
        return [_scalar(attr, v) for v in raw]
    raise ValueError("unsupported operand")


def clean_condition(target_type: str, node: Any, path: str = "condition") -> tuple[Any, list[str]]:
    """Validate a condition against the target registry.  Returns ``(normalized | None, problems)``; ``None``/empty input is "no condition"."""
    if node in (None, {}, ""):
        return None, []
    try:
        spec = get_spec(target_type)
    except Exception:  # noqa: BLE001
        return None, [f"{path}: unknown target type."]
    problems: list[str] = []
    leaves = [0]

    def walk(n: Any, where: str, depth: int) -> Any:
        if depth > MAX_DEPTH:
            problems.append(f"{where}: conditions may be nested at most {MAX_DEPTH} levels deep.")
            return None
        if not isinstance(n, dict):
            problems.append(f"{where}: a condition must be an object.")
            return None
        if "logic" in n or "conditions" in n:
            if set(n) - {"logic", "conditions"} or str(n.get("logic", "")).upper() not in ("AND", "OR") or not isinstance(n.get("conditions"), list) or not n["conditions"]:
                problems.append(f"{where}: a group is {{\"logic\": \"AND\"|\"OR\", \"conditions\": [ ... ]}}.")
                return None
            children = [walk(c, f"{where}.conditions[{i}]", depth + 1) for i, c in enumerate(n["conditions"])]
            return None if any(c is None for c in children) else {"logic": str(n["logic"]).upper(), "conditions": children}
        leaves[0] += 1
        if leaves[0] > MAX_LEAVES:
            problems.append(f"{where}: at most {MAX_LEAVES} comparisons per condition.")
            return None
        if set(n) - {"attribute", "operator", "value"}:
            problems.append(f"{where}: unexpected keys: " + ", ".join(sorted(set(n) - {"attribute", "operator", "value"})) + ".")
            return None
        attr = spec.attributes.get(str(n.get("attribute", "")))
        op = OPERATORS.get(str(n.get("operator", "")))
        if attr is None:
            problems.append(f"{where}: “{n.get('attribute')}” is not an attribute of {spec.label.lower()} records.")
            return None
        if op is None or attr.family not in op.families:
            problems.append(f"{where}: the operator “{n.get('operator')}” does not apply to {attr.label.lower()}.")
            return None
        try:
            value = _operand(attr, op, n.get("value"))
        except ValueError as exc:
            problems.append(f"{where}: the value {exc}.")
            return None
        return {"attribute": attr.code, "operator": op.code, "value": value}

    normalized = walk(node, path, 1)
    return (normalized if not problems else None), problems


def evaluate(target_type: str, node: Any, facts: dict[str, Any]) -> Tri:
    """The three-valued result of a (validated) condition over a record's facts.  ``None`` means "no condition" (TRUE)."""
    if not node:
        return Tri.TRUE
    try:
        spec = get_spec(target_type)
    except Exception:  # noqa: BLE001
        return Tri.UNKNOWN

    def walk(n: Any) -> Tri:
        if not isinstance(n, dict):
            return Tri.UNKNOWN
        if "logic" in n:
            results = [walk(c) for c in n.get("conditions") or []]
            if not results:
                return Tri.UNKNOWN
            return tri_and(results) if n["logic"] == "AND" else tri_or(results)
        attr, op = spec.attributes.get(n.get("attribute", "")), OPERATORS.get(n.get("operator", ""))
        if attr is None or op is None or attr.family not in op.families:
            return Tri.UNKNOWN  # a deleted / unsupported attribute or operator: fail closed
        value = facts.get(attr.code)
        state = AnswerState(EMPTY) if value is None else AnswerState(ANSWERED, value, value)
        return evaluate_comparison(op, attr.family, state, n.get("value"))

    return walk(node)


def describe(target_type: str, node: Any) -> str:
    """A plain-text reading of a condition for the designer and the preview."""
    if not node:
        return "always"
    try:
        spec = get_spec(target_type)
    except Exception:  # noqa: BLE001
        return "(unknown target type)"

    def walk(n: Any) -> str:
        if "logic" in n:
            parts = [walk(c) for c in n["conditions"]]
            return "(" + f" {n['logic'].lower()} ".join(parts) + ")" if len(parts) > 1 else parts[0]
        attr = spec.attributes.get(n["attribute"])
        op = OPERATORS.get(n["operator"])
        value = "" if n.get("value") is None else f" {n['value']}"
        return f"{attr.label if attr else n['attribute']} {op.label if op else n['operator']}{value}"

    return walk(node)
