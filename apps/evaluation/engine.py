"""The deterministic rule interpreter.  Pure: no database, clock or randomness - identical inputs and rules always give an identical result.

Semantics (documented in ``docs/evaluation-conditions-and-semantics.md``):

* **Three-valued logic** (Phase 6): a leaf is TRUE / FALSE / UNKNOWN (``evaluate_comparison`` over the input's answer state - a missing, N/A,
  invalid, hidden or unavailable input is UNKNOWN, never FALSE and never zero).  ``AND`` / ``OR`` are Kleene; ``NOT`` flips TRUE / FALSE and leaves
  UNKNOWN.  Only TRUE matches a rule.
* **Order:** enabled rules run by ``(priority, code)``.  A matched rule with ``stop`` ends processing; the remaining rules are recorded as *skipped*.
* **Outcome:** the highest-precedence outcome among the matched rules (``FAIL > REVIEW_REQUIRED > INDETERMINATE > WARNING > PASS``); a rule whose
  condition could not be decided and whose ``on_unknown`` is ``indeterminate`` contributes INDETERMINATE; nothing matched = PASS; no enabled rule =
  NOT_APPLICABLE.  Every matched rule stays in the result - a critical match is never discarded by a milder one.
* **Explanations** are generated here, from the actual observed values and the rule's own configuration.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from apps.checklists.rules.operators import evaluate_comparison
from apps.checklists.rules.state import ANSWERED, EMPTY, HIDDEN, INVALID, NOT_APPLICABLE, AnswerState
from apps.checklists.rules.tri import Tri, tri_and, tri_not, tri_or

from . import constants as k
from .config import CompiledRule, Group, Leaf

_STATUS_WORDS = {EMPTY: "not answered", NOT_APPLICABLE: "N/A", INVALID: "unavailable", HIDDEN: "hidden"}


@dataclass
class Inputs:
    """The normalized facts of one evaluation run (read once, reused by every condition)."""

    answers: dict[str, AnswerState] = field(default_factory=dict)
    score: dict[str, AnswerState] = field(default_factory=dict)
    execution: dict[str, AnswerState] = field(default_factory=dict)

    def state(self, source: str, name: str) -> AnswerState:
        table = {"answer": self.answers, "score": self.score, "execution": self.execution}.get(source, {})
        return table.get(name) or AnswerState(INVALID)


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Decimal):
        text = format(value.normalize(), "f")
        return "0" if text in ("-0", "") else text
    if isinstance(value, (list, tuple)):
        return ", ".join(_text(v) for v in value) or "(none)"
    return str(value)


def observed(state: AnswerState) -> str:
    return _text(state.serialized if state.serialized is not None else state.value) if state.status == ANSWERED else _STATUS_WORDS.get(state.status, state.status)


def operand_text(leaf: Leaf) -> str:
    stored = leaf.stored
    if stored is None:
        return ""
    if leaf.op.operand == "range":
        return f"{_text(stored[0])} and {_text(stored[1])}"
    return _text(stored)


def leaf_text(leaf: Leaf) -> str:
    return f"{leaf.label} {leaf.op.label}" + (f" {operand_text(leaf)}" if leaf.stored is not None else "")


@dataclass
class NodeResult:
    result: Tri
    text: str  # the condition in words, with what was observed
    kind: str  # leaf | group
    logic: str = ""
    children: list[NodeResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.kind, "result": self.result.value, "text": self.text}
        if self.kind == "group":
            out.update(logic=self.logic, children=[c.as_dict() for c in self.children])
        return out


def _eval_node(node: Any, inputs: Inputs) -> NodeResult:
    if isinstance(node, Group):
        kids = [_eval_node(c, inputs) for c in node.children]
        if node.logic == "AND":
            result, joiner = tri_and(c.result for c in kids), " and "
        elif node.logic == "OR":
            result, joiner = tri_or(c.result for c in kids), " or "
        else:
            result, joiner = tri_not(kids[0].result), ""
        text = f"it is not the case that {kids[0].text}" if node.logic == "NOT" else ("(" + joiner.join(c.text for c in kids) + ")" if len(kids) > 1 else kids[0].text)
        return NodeResult(result, text, "group", node.logic, kids)
    state = inputs.state(node.source, node.field)
    result = evaluate_comparison(node.op, node.family, state, node.parsed)
    return NodeResult(result, f"{leaf_text(node)} (observed: {observed(state)})", "leaf")


def _reason(node: NodeResult) -> str:
    """Why a TRUE condition is true, from the leaves that decided it."""
    if node.kind == "leaf":
        return node.text
    if node.logic == "NOT":
        return node.text
    if node.logic == "AND":
        return " and ".join(_reason(c) for c in node.children)
    return " or ".join(_reason(c) for c in node.children if c.result is Tri.TRUE)


def _unknown_reason(node: NodeResult) -> str:
    if node.kind == "leaf":
        return node.text
    return " and ".join(_unknown_reason(c) for c in node.children if c.result is Tri.UNKNOWN) or node.text


@dataclass
class Evaluation:
    outcome: str
    rules: list[dict[str, Any]]
    matched: list[str]
    actions: list[dict[str, Any]]
    explanation: list[str]
    undecided: list[str]
    engine_version: int = k.ENGINE_VERSION

    @property
    def critical(self) -> bool:
        return self.outcome == k.Outcome.FAIL

    def as_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "rules": self.rules, "matched": self.matched, "actions": self.actions, "explanation": self.explanation,
                "undecided": self.undecided, "engine_version": self.engine_version}


def evaluate(rules: list[CompiledRule], inputs: Inputs) -> Evaluation:
    """Evaluate ``rules`` (any order - they are sorted here by ``(priority, code)``) over ``inputs``."""
    ordered = sorted(rules, key=lambda r: (r.priority, r.code))
    results: list[dict[str, Any]] = []
    matched: list[str] = []
    undecided: list[str] = []
    actions: list[dict[str, Any]] = []
    candidates: list[str] = []
    explanation: list[str] = []
    stopped_by = ""
    enabled = [r for r in ordered if r.enabled]
    for rule in ordered:
        row: dict[str, Any] = {"code": rule.code, "name": rule.name, "priority": rule.priority, "outcome": rule.outcome}
        if not rule.enabled:
            results.append({**row, "status": k.RuleStatus.DISABLED, "explanation": f"Rule “{rule.name}” is disabled."})
            continue
        if stopped_by:
            results.append({**row, "status": k.RuleStatus.SKIPPED, "explanation": f"Rule “{rule.name}” was not evaluated because rule {stopped_by} stopped processing."})
            continue
        node = _eval_node(rule.node, inputs)
        row["conditions"] = node.as_dict()
        if node.result is Tri.TRUE:
            text = f"Rule “{rule.name}” matched because {_reason(node)}. The resulting outcome is {k.Outcome(rule.outcome).label.upper()}."
            if rule.message:
                text += f" {rule.message}"
            matched.append(rule.code)
            candidates.append(rule.outcome)
            explanation.append(text)
            results.append({**row, "status": k.RuleStatus.MATCHED, "explanation": text, "message": rule.message})
            for index, action in enumerate(rule.actions):
                actions.append({"rule": rule.code, "rule_name": rule.name, "index": index, "action": action})
            if rule.stop:
                stopped_by = rule.code
        elif node.result is Tri.UNKNOWN:
            text = f"Rule “{rule.name}” could not be decided because {_unknown_reason(node)}."
            undecided.append(rule.code)
            if rule.on_unknown == k.OnUnknown.INDETERMINATE:
                candidates.append(k.Outcome.INDETERMINATE)
                text += " The rule makes the evaluation indeterminate."
                explanation.append(text)
            results.append({**row, "status": k.RuleStatus.UNKNOWN, "explanation": text})
        else:
            results.append({**row, "status": k.RuleStatus.UNMATCHED, "explanation": f"Rule “{rule.name}” did not match: {node.text}."})
    if not enabled:
        outcome = k.Outcome.NOT_APPLICABLE
        explanation.append("The rule set has no enabled rule, so there is nothing to evaluate.")
    elif not candidates:
        outcome = k.Outcome.PASS
        explanation.append("No rule matched, so the evaluation passes.")
    else:
        outcome = max((k.Outcome(c) for c in candidates), key=lambda o: k.PRECEDENCE[o])
        if len(set(candidates)) > 1:
            explanation.append(f"Several outcomes were requested ({', '.join(sorted(set(candidates)))}); the highest-precedence one, {outcome.label.upper()}, applies and every match is listed.")
    return Evaluation(str(outcome), results, matched, actions, explanation, undecided)


def input_fingerprint(inputs: Inputs) -> str:
    """A stable digest of exactly the facts that were read (source, field, status and value), independent of key order."""
    rows = []
    for source, table in (("answer", inputs.answers), ("score", inputs.score), ("execution", inputs.execution)):
        for name in sorted(table):
            state = table[name]
            rows.append([source, name, state.status, _text(state.serialized if state.serialized is not None else state.value) if state.status == ANSWERED else ""])
    return hashlib.sha256(json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
