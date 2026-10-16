"""Validation and compilation of one rule (condition tree + actions) against a checklist version.

The accepted shape is intentionally small and closed::

    condition: {"operator": "AND"|"OR", "conditions": [ <condition> | <group>, ... ]}
    condition: {"source_item_code": "FIRE_EXIT", "comparison": "EQUALS", "value": "no"}
    action:    {"type": "REQUIRE_COMMENT", "target": {"type": "item", "code": "FIRE_EXIT"}, ...params}

Unknown keys are errors (a rule cannot smuggle extra behaviour), every ``source_item_code`` must exist in *this*
version, every comparison must be one of the operators the source item's answer type allows, and every comparison value
is parsed by the source answer type's own rules (Phase 5).  All problems of a rule are collected, not just the first.
"""

from __future__ import annotations

from typing import Any

from .actions import clean_action
from .compiled import CompiledAction, CompiledRule, Condition, Group
from .errors import Issue
from .limits import limit
from .operators import OPERATORS, OperandError, clean_operand, operators_for
from .snapshot import RuleSpec, VersionSnapshot

_CONDITION_KEYS = {"source_item_code", "comparison", "value"}
_GROUP_KEYS = {"operator", "conditions"}


class _Counter:
    def __init__(self) -> None:
        self.conditions = 0


def _clean_node(
    snapshot: VersionSnapshot, raw: Any, path: str, depth: int, counter: _Counter, issues: list[Issue], rule: str
) -> tuple[dict[str, Any] | None, Condition | Group | None]:
    def fail(message: str, sub: str = "", code: str = "invalid_condition") -> None:
        issues.append(Issue("error", code, message, rule=rule, path=f"{path}{sub}"))

    if not isinstance(raw, dict):
        fail("A condition must be an object.")
        return None, None
    if "conditions" in raw or "operator" in raw:  # a group
        unknown = sorted(set(raw) - _GROUP_KEYS)
        if unknown:
            fail("Unexpected keys in a group: " + ", ".join(unknown) + ".")
        if depth > limit("max_group_depth"):
            fail(f"Groups may be nested at most {limit('max_group_depth')} levels deep.", code="too_deep")
            return None, None
        logic = str(raw.get("operator", "")).strip().upper()
        if logic not in ("AND", "OR"):
            fail("A group's operator must be AND or OR.", ".operator")
            logic = ""
        children = raw.get("conditions")
        if not isinstance(children, list) or not children:
            fail("A group needs at least one condition.", ".conditions")
            return None, None
        stored: list[dict[str, Any]] = []
        compiled: list[Condition | Group] = []
        for index, child in enumerate(children):
            s, c = _clean_node(snapshot, child, f"{path}.conditions[{index}]", depth + 1, counter, issues, rule)
            if s is not None and c is not None:
                stored.append(s)
                compiled.append(c)
        if not logic or len(stored) != len(children):
            return None, None
        return {"operator": logic, "conditions": stored}, Group(logic, tuple(compiled))
    # a single condition
    counter.conditions += 1
    if counter.conditions == limit("max_conditions_per_rule") + 1:
        fail(f"A rule may have at most {limit('max_conditions_per_rule')} conditions.", code="too_many")
    unknown = sorted(set(raw) - _CONDITION_KEYS)
    if unknown:
        fail("Unexpected keys in a condition: " + ", ".join(unknown) + ".")
        return None, None
    code = raw.get("source_item_code")
    item = snapshot.items.get(code) if isinstance(code, str) else None
    if item is None:
        fail(f"Unknown question “{code}” in this checklist version.", ".source_item_code", "unknown_item")
        return None, None
    name = str(raw.get("comparison", "")).strip().upper()
    allowed = operators_for(item.answer_type)
    operator = OPERATORS.get(name)
    if operator is None or operator not in allowed:
        fail(
            f"“{name or '(none)'}” is not available for {item.definition.label} questions. "
            f"Use one of: {', '.join(o.code for o in allowed)}.", ".comparison", "invalid_operator",
        )
        return None, None
    try:
        stored_value, parsed = clean_operand(operator, item, raw.get("value"))
    except OperandError as exc:
        fail(str(exc), ".value", "invalid_value")
        return None, None
    out: dict[str, Any] = {"source_item_code": item.code, "comparison": operator.code}
    if operator.operand != "none":
        out["value"] = stored_value
    return out, Condition(item.code, operator, item.definition.comparison_family, parsed, stored_value)


def compile_rule(snapshot: VersionSnapshot, rule: RuleSpec | dict[str, Any]) -> tuple[CompiledRule | None, list[Issue]]:
    """Validate and compile ``rule``.  Returns ``(compiled, issues)``; ``compiled`` is ``None`` if any error was found."""
    data = rule if isinstance(rule, dict) else {
        "code": rule.code, "name": rule.name, "description": rule.description, "condition": rule.condition,
        "actions": rule.actions, "is_active": rule.is_active, "sort_order": rule.sort_order,
    }
    code = str(data.get("code", ""))
    issues: list[Issue] = []

    def fail(message: str, path: str = "", issue_code: str = "invalid_rule") -> None:
        issues.append(Issue("error", issue_code, message, rule=code, path=path))

    # -- condition ---------------------------------------------------------------------------
    raw_condition = data.get("condition")
    stored_condition: dict[str, Any] | None = None
    compiled_condition: Group | None = None
    if raw_condition not in (None, {}, [], ""):
        if isinstance(raw_condition, dict) and "operator" not in raw_condition and "conditions" not in raw_condition:
            raw_condition = {"operator": "AND", "conditions": [raw_condition]}  # a bare condition: wrap it
        if isinstance(raw_condition, dict) and raw_condition.get("conditions") == []:
            raw_condition = None  # an empty root group means "always"
        if raw_condition is not None:
            stored_condition, node = _clean_node(snapshot, raw_condition, "condition", 1, _Counter(), issues, code)
            compiled_condition = node if isinstance(node, Group) else None
    # -- actions -----------------------------------------------------------------------------
    raw_actions = data.get("actions")
    stored_actions: list[dict[str, Any]] = []
    compiled_actions: list[CompiledAction] = []
    if not isinstance(raw_actions, list) or not raw_actions:
        fail("A rule needs at least one action.", "actions")
    elif len(raw_actions) > limit("max_actions_per_rule"):
        fail(f"A rule may have at most {limit('max_actions_per_rule')} actions.", "actions", "too_many")
    else:
        for index, raw_action in enumerate(raw_actions):
            normalized, parts, action_issues = clean_action(snapshot, raw_action, f"actions[{index}]")
            issues.extend(Issue(i.level, i.code, i.message, rule=code, path=i.path) for i in action_issues)
            if parts is not None:
                kind_type, kind, target, params = parts
                stored_actions.append(normalized)
                compiled_actions.append(CompiledAction(kind_type, kind, target, params))
    if any(i.level == "error" for i in issues):
        return None, issues
    return CompiledRule(
        code=code, name=str(data.get("name", "")), description=str(data.get("description", "")),
        is_active=bool(data.get("is_active", True)), sort_order=int(data.get("sort_order", 0) or 0),
        condition=compiled_condition, actions=tuple(compiled_actions),
        stored_condition=stored_condition, stored_actions=stored_actions,
    ), issues
