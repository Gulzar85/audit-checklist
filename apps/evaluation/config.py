"""Evaluation rules as data: strict schema, compilation, validation and the canonical (hashable) form.  Pure - no database access.

A rule set is ``{"rules": [rule, ...]}``.  A rule::

    {"code": "LOW-SCORE", "name": "Low score review", "description": "", "enabled": true, "priority": 20,
     "condition": <node>, "outcome": "review_required", "message": "...", "on_unknown": "skip", "stop": false, "actions": [<action>, ...]}

A node is a *leaf* ``{"source": "answer"|"score"|"execution", "field": ..., "operator": ..., "value": ...}`` or a *group*
``{"logic": "AND"|"OR"|"NOT", "conditions": [...]}`` (``NOT`` has exactly one child).  Operators and their per-family applicability are the Phase 6
registry (``apps.checklists.rules.operators``); answer comparison values are validated by the question's own answer type (``clean_operand``);
score / execution fields come from ``sources.py``.  Nothing is parsed from text and nothing is executed: an unknown key, source, field, operator or
action is an error, and a compiled rule can only be evaluated by ``engine.py``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from apps.checklists.rules.operators import OPERATORS, OperandError as AnswerOperandError, clean_operand, operators_for

from . import constants as k
from . import sources
from .sources import OperandError

_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")
RULE_KEYS = {"code", "name", "description", "enabled", "priority", "condition", "outcome", "message", "on_unknown", "stop", "actions"}


@dataclass(frozen=True)
class Issue:
    level: str  # error | warning
    code: str
    message: str
    subject: str  # e.g. "rule:LOW-SCORE", "rules", "settings"

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message, "subject": self.subject}


@dataclass(frozen=True)
class Leaf:
    source: str
    field: str
    op: Any  # OperatorDef
    family: str
    parsed: Any
    stored: Any
    label: str  # readable name of the compared thing


@dataclass(frozen=True)
class Group:
    logic: str  # AND | OR | NOT
    children: tuple[Any, ...]


@dataclass(frozen=True)
class CompiledRule:
    code: str
    name: str
    description: str
    enabled: bool
    priority: int
    node: Any
    outcome: str
    message: str
    on_unknown: str
    stop: bool
    actions: tuple[dict[str, Any], ...]
    stored: dict[str, Any]


class Lookups:
    """What the *organization* defines (finding categories / severities, workflows): supplied by the service layer so this module stays pure."""

    def category(self, code: str) -> bool:  # pragma: no cover - interface
        return True

    def severity(self, code: str) -> bool:  # pragma: no cover - interface
        return True

    def workflow(self, code: str) -> bool:  # pragma: no cover - interface
        return True


def empty_document() -> dict[str, Any]:
    return {"rules": []}


def canonical(enabled: bool, document: dict[str, Any]) -> dict[str, Any]:
    return {"enabled": bool(enabled), "rules": (document or {}).get("rules") or []}


def ruleset_hash(enabled: bool, document: dict[str, Any]) -> str:
    text = json.dumps(canonical(enabled, document), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------------------------------------------------------------
class _Counter:
    leaves = 0


def _compile_node(snapshot: Any, raw: Any, path: str, depth: int, counter: _Counter, err: Any) -> tuple[Any, Any]:
    """``(stored, compiled)`` or ``(None, None)`` after reporting through ``err``."""
    if depth > k.MAX_DEPTH:
        err("too_deep", f"conditions may be nested at most {k.MAX_DEPTH} levels deep", path)
        return None, None
    if not isinstance(raw, dict):
        err("malformed", "a condition must be an object", path)
        return None, None
    if "logic" in raw or "conditions" in raw:
        logic = str(raw.get("logic", "")).upper()
        children = raw.get("conditions")
        if set(raw) - {"logic", "conditions"} or logic not in ("AND", "OR", "NOT") or not isinstance(children, list) or not children:
            err("malformed", 'a group is {"logic": "AND"|"OR"|"NOT", "conditions": [...]}', path)
            return None, None
        if logic == "NOT" and len(children) != 1:
            err("malformed", "NOT negates exactly one condition", path)
            return None, None
        if len(children) > k.MAX_CONDITIONS:
            err("too_many_conditions", f"a group may hold at most {k.MAX_CONDITIONS} conditions", path)
            return None, None
        pairs = [_compile_node(snapshot, c, f"{path}.conditions[{i}]", depth + 1, counter, err) for i, c in enumerate(children)]
        if any(s is None for s, _c in pairs):
            return None, None
        return {"logic": logic, "conditions": [s for s, _c in pairs]}, Group(logic, tuple(c for _s, c in pairs))
    counter.leaves += 1
    if counter.leaves > k.MAX_CONDITIONS:
        if counter.leaves == k.MAX_CONDITIONS + 1:
            err("too_many_conditions", f"a rule may have at most {k.MAX_CONDITIONS} comparisons", path)
        return None, None
    extra = set(raw) - {"source", "field", "operator", "value"}
    if extra:
        err("malformed", "unexpected keys: " + ", ".join(sorted(extra)), path)
        return None, None
    source, field_code = str(raw.get("source", "")), str(raw.get("field", ""))
    op = OPERATORS.get(str(raw.get("operator", "")).upper())
    if source not in k.SOURCES:
        err("unknown_source", f"“{source}” is not an input source (use: {', '.join(k.SOURCES)})", path)
        return None, None
    if op is None:
        err("unknown_operator", f"“{raw.get('operator')}” is not a supported operator", path)
        return None, None
    if source == "answer":
        item = snapshot.items.get(field_code)
        if item is None:
            err("unknown_item", f"“{field_code}” is not a question of this checklist version", path)
            return None, None
        if op not in operators_for(item.answer_type):
            allowed = ", ".join(o.code for o in operators_for(item.answer_type))
            err("invalid_operator", f"{op.code} is not available for {item.definition.label} questions (use: {allowed})", path)
            return None, None
        try:
            stored, parsed = clean_operand(op, item, raw.get("value"))
        except AnswerOperandError as exc:
            err("invalid_value", str(exc), path)
            return None, None
        family, label = item.definition.comparison_family, f"the answer to “{item.question}” ({item.code})"
    else:
        field = sources.FIELDS[source].get(field_code)
        if field is None:
            err("unknown_field", f"“{field_code}” is not a field of the {source} source", path)
            return None, None
        if field.family not in op.families:
            err("invalid_operator", f"{op.code} does not apply to {field.label.lower()}", path)
            return None, None
        try:
            stored, parsed = sources.parse_field_operand(field, op, raw.get("value"))
        except OperandError as exc:
            err("invalid_value", f"the value {exc}", path)
            return None, None
        family, label = field.family, field.label.lower()
    node: dict[str, Any] = {"source": source, "field": field_code, "operator": op.code}
    if op.operand != "none":
        node["value"] = stored
    return node, Leaf(source, field_code, op, family, parsed, stored, label)


# ---------------------------------------------------------------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------------------------------------------------------------
def _clean_action(raw: Any, index: int, path: str, lookups: Lookups | None, err: Any) -> dict[str, Any] | None:
    where = f"{path}.actions[{index}]"
    if not isinstance(raw, dict):
        err("malformed", "an action must be an object", where)
        return None
    kind = str(raw.get("type", ""))
    if kind not in k.ActionType.values:
        err("unknown_action", f"“{kind}” is not a supported action (use: {', '.join(k.ActionType.values)})", where)
        return None
    if kind == k.ActionType.REQUEST_FINDING:
        allowed = {"type", "category", "severity", "title", "description", "requires_review"}
    elif kind == k.ActionType.START_WORKFLOW:
        allowed = {"type", "workflow"}
    else:
        allowed = {"type", "tag"}
    if set(raw) - allowed:
        err("malformed", "unexpected keys: " + ", ".join(sorted(set(raw) - allowed)), where)
        return None
    out: dict[str, Any] = {"type": kind}
    ok = True
    if kind == k.ActionType.REQUEST_FINDING:
        for key in ("category", "severity"):
            value = str(raw.get(key, "")).strip()
            if not _SLUG.match(value):
                err("invalid_action", f"a finding request needs a {key} code", where)
                ok = False
            elif lookups is not None and not (lookups.category(value) if key == "category" else lookups.severity(value)):
                err("unknown_reference", f"“{value}” is not an active finding {key} of this organization", where)
                ok = False
            out[key] = value
        title = str(raw.get("title", "")).strip()
        if not title or len(title) > 200:
            err("invalid_action", "a finding request needs a title of 1 to 200 characters", where)
            ok = False
        out["title"] = title
        description = str(raw.get("description", "")).strip()
        if len(description) > 2000:
            err("invalid_action", "the finding description can have at most 2000 characters", where)
            ok = False
        out["description"] = description
        if "requires_review" in raw and not isinstance(raw["requires_review"], bool):
            err("invalid_action", "requires_review must be true or false", where)
            ok = False
        out["requires_review"] = bool(raw.get("requires_review", False))
    elif kind == k.ActionType.START_WORKFLOW:
        value = str(raw.get("workflow", "")).strip()
        if not _SLUG.match(value):
            err("invalid_action", "a workflow request needs the workflow code", where)
            ok = False
        elif lookups is not None and not lookups.workflow(value):
            err("unknown_reference", f"“{value}” is not a published workflow for checklist executions of this organization", where)
            ok = False
        out["workflow"] = value
    else:
        value = str(raw.get("tag", "")).strip()
        if not _TAG.match(value):
            err("invalid_action", f"a tag is 1 to {k.MAX_TAG} letters, digits, spaces, dots, dashes or underscores", where)
            ok = False
        out["tag"] = value
    return out if ok else None


# ---------------------------------------------------------------------------------------------------------------------------------
# Rules and the whole document
# ---------------------------------------------------------------------------------------------------------------------------------
def compile_rule(snapshot: Any, raw: Any, position: int, lookups: Lookups | None, issues: list[Issue]) -> CompiledRule | None:
    """Validate one rule.  Problems are appended to ``issues``; returns ``None`` when the rule has any error."""
    code_hint = raw.get("code") if isinstance(raw, dict) else None
    subject = f"rule:{code_hint}" if code_hint else f"rule:#{position}"
    before = sum(1 for i in issues if i.level == "error")

    def err(code: str, message: str, path: str = "") -> None:
        issues.append(Issue("error", code, f"{message}" if not path or path == subject else f"{path}: {message}", subject))

    if not isinstance(raw, dict):
        err("malformed", "a rule must be an object")
        return None
    extra = set(raw) - RULE_KEYS
    if extra:
        err("malformed", "unexpected keys: " + ", ".join(sorted(extra)))
    code = str(raw.get("code", "")).strip()
    if not _CODE.match(code):
        err("invalid_code", "a rule code is 1 to 40 letters, digits, dots, dashes or underscores")
    name = str(raw.get("name", "")).strip()
    if not name or len(name) > 120:
        err("invalid_name", "a rule needs a name of 1 to 120 characters")
    description = str(raw.get("description", "")).strip()
    if len(description) > 500:
        err("invalid_description", "the description can have at most 500 characters")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        err("invalid_enabled", "enabled must be true or false")
    priority = raw.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100000:
        err("invalid_priority", "the priority is a whole number from 0 to 100000 (lower runs first)")
    outcome = str(raw.get("outcome", ""))
    if outcome not in k.RULE_OUTCOMES:
        err("invalid_outcome", f"the outcome must be one of: {', '.join(k.RULE_OUTCOMES)}")
    message = str(raw.get("message", "")).strip()
    if len(message) > k.MAX_MESSAGE:
        err("invalid_message", f"the message can have at most {k.MAX_MESSAGE} characters")
    on_unknown = str(raw.get("on_unknown", k.OnUnknown.SKIP))
    if on_unknown not in k.OnUnknown.values:
        err("invalid_on_unknown", f"on_unknown must be one of: {', '.join(k.OnUnknown.values)}")
    stop = raw.get("stop", False)
    if not isinstance(stop, bool):
        err("invalid_stop", "stop must be true or false")
    raw_actions = raw.get("actions", [])
    actions: list[dict[str, Any]] = []
    if not isinstance(raw_actions, list) or len(raw_actions) > k.MAX_ACTIONS:
        err("invalid_actions", f"actions is a list of at most {k.MAX_ACTIONS}")
    else:
        for i, a in enumerate(raw_actions):
            cleaned = _clean_action(a, i, subject, lookups, lambda c, m, p: issues.append(Issue("error", c, f"{p}: {m}", subject)))
            if cleaned is not None:
                actions.append(cleaned)
        if len(actions) != len(raw_actions):
            pass  # the individual errors are already reported
    if not raw.get("condition"):
        err("condition_required", "a rule needs a condition")
        stored_node = node = None
    else:
        stored_node, node = _compile_node(snapshot, raw["condition"], f"{subject}.condition", 1, _Counter(), lambda c, m, p: issues.append(Issue("error", c, f"{p}: {m}", subject)))
    if sum(1 for i in issues if i.level == "error") > before:
        return None
    stored = {"code": code, "name": name, "description": description, "enabled": enabled, "priority": priority, "condition": stored_node, "outcome": outcome,
              "message": message, "on_unknown": on_unknown, "stop": stop, "actions": actions}
    return CompiledRule(code, name, description, enabled, priority, node, outcome, message, on_unknown, stop, tuple(actions), stored)


def validate_document(enabled: Any, document: Any, snapshot: Any, lookups: Lookups | None = None) -> tuple[list[Issue], list[CompiledRule], dict[str, Any]]:
    """``(issues, compiled rules, normalized document)`` of a rule set.  Never raises.  The normalized document is empty when malformed."""
    issues: list[Issue] = []
    if not isinstance(enabled, bool):
        return [Issue("error", "invalid_enabled", "enabled must be true or false", "settings")], [], empty_document()
    if not isinstance(document, dict) or set(document) - {"rules"} or not isinstance(document.get("rules", []), list):
        return [Issue("error", "malformed", 'the rule set must be {"rules": [ ... ]}', "rules")], [], empty_document()
    raw_rules = document.get("rules", [])
    if len(raw_rules) > k.MAX_RULES:
        return [Issue("error", "too_many_rules", f"a rule set may have at most {k.MAX_RULES} rules", "rules")], [], empty_document()
    compiled: list[CompiledRule] = []
    for position, raw in enumerate(raw_rules, 1):
        rule = compile_rule(snapshot, raw, position, lookups, issues)
        if rule is not None:
            compiled.append(rule)
    seen_codes: dict[str, int] = {}
    seen_priority: dict[int, str] = {}
    for raw in raw_rules:  # over every rule, valid or not: a duplicate is an error whatever else is wrong with the rule
        if not isinstance(raw, dict):
            continue
        code, priority = str(raw.get("code", "")).strip(), raw.get("priority")
        if code:
            seen_codes[code] = seen_codes.get(code, 0) + 1
        if isinstance(priority, int) and not isinstance(priority, bool):
            if priority in seen_priority:
                issues.append(Issue("error", "duplicate_priority", f"Rules {seen_priority[priority]} and {code or '?'} share priority {priority}; give each rule its own priority so the order is explicit",
                                    f"rule:{code}"))
            else:
                seen_priority[priority] = code or "?"
    for code, count in seen_codes.items():
        if count > 1:
            issues.append(Issue("error", "duplicate_code", f"The rule code {code} is used more than once", f"rule:{code}"))
    if enabled and not any(r.enabled for r in compiled) and not any(i.level == "error" for i in issues):
        issues.append(Issue("warning", "no_enabled_rules", "No rule is enabled, so every evaluation is “not applicable”", "rules"))
    for rule in compiled:
        if rule.enabled and not rule.message:
            issues.append(Issue("warning", "no_message", f"Rule {rule.code} has no message; the explanation is generated from its conditions", f"rule:{rule.code}"))
    ordered = sorted(compiled, key=lambda r: (r.priority, r.code))
    return issues, ordered, {"rules": [r.stored for r in ordered]}
