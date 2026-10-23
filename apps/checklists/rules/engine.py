"""The rule evaluator: explicit inputs in, a deterministic effective state out.

``compile_rules(snapshot)`` validates and indexes a version's rules once (reuse the result while the rules are unchanged);
``evaluate(compiled, answers, not_applicable)`` computes, for the given answers, which sections and questions are visible,
which are required, which options and limits apply, which messages / comment / evidence requirements are active, and
whether the checklist is complete.  Nothing here reads or writes the database, uses the clock or any global state, and
nothing is executed: conditions and actions are interpreted from the operator and action registries.

Evaluation order (deterministic, independent of database order)
1. Source answers are resolved with the **Phase 5 validators** - the engine never parses answers itself.
2. Nodes (sections, then their questions) are processed in dependency order (``graph.topological_order``).  For each
   node the *graph-edge* rules targeting it (show/hide, option availability, validation constraints) are evaluated -
   their sources are already final - and the node's effective answer is recomputed (a hidden question's answer is
   ignored, an answer outside the effective options/limits becomes invalid).
3. All other rules are evaluated against the final answer states and annotate the result (required / optional, messages,
   comment and evidence requirements, instructions).
Conflicts are resolved per property by the documented precedence: **hide beats show; required beats optional;
options are intersected (restrictive); constraints take the tightest value; messages and instructions accumulate in
rule order**.  Only a rule whose condition is TRUE acts; UNKNOWN never does.

Hidden is not deleted: a hidden question's answer is neither validated nor counted for completion, but the caller keeps it
(it is simply ignored until the question is visible again).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..answer_types import ANSWERED as A_ANSWERED
from ..answer_types import EMPTY as A_EMPTY
from ..answer_types import INVALID as A_INVALID
from ..answer_types import validate_in_context
from .actions import ACTIONS, EVIDENCE_KINDS
from .compiled import CompiledAction, CompiledRule, Condition, Group
from .constraints import tighten
from .errors import Issue
from .graph import build_graph, describe_cycle, find_cycle, topological_order
from .operators import evaluate_comparison
from .snapshot import ItemSpec, VersionSnapshot
from .state import ANSWERED, EMPTY, HIDDEN, INVALID, NOT_APPLICABLE, AnswerState
from .tri import Tri, tri_and, tri_or
from .validation import validate_rule_set


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------
@dataclass
class CompiledRuleSet:
    snapshot: VersionSnapshot
    rules: list[CompiledRule]  # every rule that compiled (enabled or not), in evaluation order
    issues: list[Issue]
    order: list[tuple[str, str]]  # dependency order of nodes (empty if the set has a cycle)
    cyclic: bool
    #: node -> [(rule, action)] for graph-edge actions
    edge_actions: dict[tuple[str, str], list[tuple[CompiledRule, CompiledAction]]] = field(default_factory=dict)

    @property
    def active(self) -> list[CompiledRule]:
        return [r for r in self.rules if r.is_active]


def compile_rules(snapshot: VersionSnapshot) -> CompiledRuleSet:
    report = validate_rule_set(snapshot)
    rules = sorted(report.compiled, key=lambda r: (r.sort_order, r.code))
    active = [r for r in rules if r.is_active]
    graph = build_graph(snapshot, active)
    cyclic = find_cycle(graph) is not None
    edge_actions: dict[tuple[str, str], list[tuple[CompiledRule, CompiledAction]]] = {}
    for rule in active:
        for action in rule.actions:
            if ACTIONS[action.type].edge:
                edge_actions.setdefault((action.target_kind, action.target_code), []).append((rule, action))
    return CompiledRuleSet(snapshot, rules, report.issues, [] if cyclic else topological_order(graph), cyclic, edge_actions)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class ItemResult:
    code: str
    section: str
    visible: bool = True
    required: bool = False  # effective: required AND visible
    base_required: bool = False
    comment_required: bool = False
    comment_prompts: list[str] = field(default_factory=list)
    evidence: dict[str, Any] | None = None
    options: dict[str, list[str]] | None = None
    constraints: dict[str, Any] | None = None
    instructions: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    #: ``RAISE_FINDING`` actions whose rule matched (Phase 12): ``{"rule", "rule_name", "index", **params}``.  Only a *visible* item raises.
    findings: list[dict[str, Any]] = field(default_factory=list)
    answer: AnswerState = field(default_factory=lambda: AnswerState(EMPTY))
    required_by_rule: bool = False
    optional_by_rule: bool = False

    def as_dict(self) -> dict[str, Any]:
        data = {
            "visible": self.visible, "required": self.required, "comment_required": self.comment_required,
            "comment_prompts": self.comment_prompts, "evidence": self.evidence, "options": self.options,
            "constraints": self.constraints, "instructions": self.instructions, "messages": self.messages,
            "answer": self.answer.as_dict(),
        }
        if self.findings:
            data["findings"] = self.findings
        return data


@dataclass
class Evaluation:
    items: dict[str, ItemResult]
    sections: dict[str, dict[str, Any]]
    rules: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    errors: list[dict[str, str]]
    completion: dict[str, Any]
    trace: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": {code: r.as_dict() for code, r in self.items.items()},
            "sections": self.sections,
            "rules": self.rules,
            "messages": self.messages,
            "errors": self.errors,
            "completion": self.completion,
            "trace": self.trace,
        }


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
def _state_from(item: ItemSpec, raw: Any, *, present: bool, na: bool, config=None, options=None) -> AnswerState:  # noqa: ANN001
    if na:
        return AnswerState(NOT_APPLICABLE)
    if not present:
        return AnswerState(EMPTY)
    result = validate_in_context(item.context(config=config, options=options), raw)
    if result.status == A_ANSWERED:
        return AnswerState(ANSWERED, result.value, result.serialized)
    if result.status == A_EMPTY:
        return AnswerState(EMPTY)
    assert result.status == A_INVALID
    return AnswerState(INVALID, errors=tuple(e.as_dict() for e in result.errors))


def _eval_node(node: Condition | Group, states: Mapping[str, AnswerState]) -> Tri:
    if isinstance(node, Condition):
        return evaluate_comparison(node.operator, node.family, states[node.item_code], node.operand)
    results = [_eval_node(child, states) for child in node.children]
    return tri_and(results) if node.logic == "AND" else tri_or(results)


def evaluate(
    compiled: CompiledRuleSet, answers: Mapping[str, Any], not_applicable: Iterable[str] = ()
) -> Evaluation:
    """Evaluate the rules for ``answers`` (raw values by question code) and ``not_applicable`` (codes declared N/A)."""
    snapshot = compiled.snapshot
    errors = [i.as_dict() for i in compiled.issues if i.level == "error"]
    na_codes = set(not_applicable)
    for code in sorted((set(answers) | na_codes) - set(snapshot.items)):
        errors.append({"level": "error", "code": "unknown_item", "message": f"Answer for unknown question “{code}” ignored.", "path": code})

    # Base answer states (before any rule): validated by the Phase 5 engine.
    states: dict[str, AnswerState] = {}
    for item in snapshot.items.values():
        if not item.active:
            states[item.code] = AnswerState(HIDDEN)
        else:
            states[item.code] = _state_from(item, answers.get(item.code), present=item.code in answers, na=item.code in na_codes)

    results = {code: ItemResult(code, item.section_code, base_required=item.required, required=item.required)
               for code, item in snapshot.items.items()}
    section_visible = {code: True for code in snapshot.sections}
    rule_results: dict[str, Tri] = {}
    trace: list[dict[str, Any]] = []

    if compiled.cyclic:  # never evaluate an unsafe rule set
        errors.append({"level": "error", "code": "cycle", "message": "The rule set contains a dependency cycle; no rule was applied."})
        active: list[CompiledRule] = []
    else:
        active = compiled.active

        def result_of(rule: CompiledRule) -> Tri:
            if rule.code not in rule_results:
                rule_results[rule.code] = Tri.TRUE if rule.condition is None else _eval_node(rule.condition, states)
            return rule_results[rule.code]

        # -- phase A: graph-edge rules in dependency order -----------------------------------------------
        for node in compiled.order:
            kind, code = node
            matched = [(r, a) for r, a in compiled.edge_actions.get(node, []) if result_of(r) is Tri.TRUE]
            declared = compiled.edge_actions.get(node, [])
            shown = any(a.type in ("SHOW_ITEM", "SHOW_SECTION") for _, a in declared)
            visible = (not shown) or any(a.type in ("SHOW_ITEM", "SHOW_SECTION") for _, a in matched)
            if any(a.type in ("HIDE_ITEM", "HIDE_SECTION") for _, a in matched):
                visible = False
            if declared and any(a.type.endswith(("_ITEM", "_SECTION")) for _, a in declared):
                reasons = [f"{a.type} by {r.code}" for r, a in matched if a.type.endswith(("_ITEM", "_SECTION"))]
                trace.append({"target": f"{kind}:{code}", "property": "visibility", "value": visible,
                              "because": reasons or ["no show rule matched"]})
            if kind == "section":
                section_visible[code] = visible
                continue
            item = snapshot.items[code]
            result = results[code]
            result.visible = visible and section_visible[item.section_code] and item.active
            config, options = item.config, None
            option_rules = [(r, a) for r, a in matched if a.type == "OPTION_AVAILABILITY"]
            if option_rules:
                allowed = [o.value for o in item.options if o.is_active]
                for _r, action in option_rules:
                    picked = set(action.params["options"])
                    allowed = [v for v in allowed if (v in picked) == (action.params["mode"] == "only")]
                everything = [o.value for o in item.options if o.is_active]
                if not allowed:
                    errors.append({"level": "error", "code": "options_exhausted", "path": code,
                                   "message": f"The rules leave no option available for {code}; all options stay available."})
                    allowed = everything
                options = tuple(o for o in item.options if o.value in allowed)
                result.options = {"available": allowed, "unavailable": [v for v in everything if v not in allowed]}
                trace.append({"target": f"item:{code}", "property": "options", "value": result.options,
                              "because": [f"OPTION_AVAILABILITY by {r.code}" for r, _a in option_rules]})
            constraint_rules = [(r, a) for r, a in matched if a.type == "SET_VALIDATION_CONSTRAINT"]
            if constraint_rules:
                config, conflict = tighten(item, [a.params["constraints"] for _r, a in constraint_rules])
                if conflict:
                    errors.append({"level": "error", "code": "constraint_conflict", "path": code, "message": conflict})
                else:
                    touched = {k for _r, a in constraint_rules for k in a.params["constraints"]}
                    result.constraints = {k: config[k] for k in sorted(touched)}
                    trace.append({"target": f"item:{code}", "property": "constraints", "value": result.constraints,
                                  "because": [f"SET_VALIDATION_CONSTRAINT by {r.code}" for r, _a in constraint_rules]})
            if not result.visible:
                states[code] = AnswerState(HIDDEN)
            elif option_rules or constraint_rules:
                states[code] = _state_from(item, answers.get(code), present=code in answers, na=code in na_codes,
                                           config=config, options=options)

        # -- phase B: every other rule, against the final answer states -------------------------------------
        for rule in active:
            result_of(rule)

    # Hidden-by-section for items without edge actions (their node was still processed above; this covers cyclic sets).
    for code, item in snapshot.items.items():
        results[code].visible = results[code].visible and section_visible[item.section_code] and item.active
    # -- aggregate annotations -------------------------------------------------------------------------------
    messages: list[dict[str, Any]] = []
    for rule in active:
        if rule_results.get(rule.code) is not Tri.TRUE:
            continue
        for index, action in enumerate(rule.actions):
            definition = ACTIONS[action.type]
            if definition.edge:
                continue
            target_item = results.get(action.target_code) if action.target_kind == "item" else None
            if action.target_kind == "item" and not target_item.visible:  # type: ignore[union-attr]
                continue
            if action.target_kind == "section" and not section_visible.get(action.target_code, True):
                continue
            if action.type == "REQUIRE_ITEM":
                target_item.required_by_rule = True  # type: ignore[union-attr]
            elif action.type == "MAKE_OPTIONAL":
                target_item.optional_by_rule = True  # type: ignore[union-attr]
            elif action.type == "REQUIRE_COMMENT":
                target_item.comment_required = True  # type: ignore[union-attr]
                if action.params.get("prompt"):
                    target_item.comment_prompts.append(action.params["prompt"])  # type: ignore[union-attr]
            elif action.type == "REQUIRE_EVIDENCE":
                current = target_item.evidence or {"kinds": [], "min_count": 1}  # type: ignore[union-attr]
                kinds = sorted(set(current["kinds"]) | set(action.params["kinds"]), key=EVIDENCE_KINDS.index)
                target_item.evidence = {"kinds": kinds, "min_count": max(current["min_count"], action.params["min_count"])}  # type: ignore[union-attr]
            elif action.type == "RAISE_FINDING":
                target_item.findings.append({"rule": rule.code, "rule_name": rule.name, "index": index, **action.params})  # type: ignore[union-attr]
            elif action.type == "ADD_INSTRUCTIONS":
                target_item.instructions.append(action.params["text"])  # type: ignore[union-attr]
            elif action.type == "DISPLAY_MESSAGE":
                message = {"severity": action.params["severity"], "message": action.params["message"], "rule": rule.code,
                           "target": {"type": action.target_kind, "code": action.target_code} if action.target_kind else None}
                messages.append(message)
                if target_item is not None:
                    target_item.messages.append(message)
    for code, result in results.items():
        optional, forced = result.optional_by_rule, result.required_by_rule
        flag = True if forced else False if optional else result.base_required  # required beats optional
        if forced or optional:
            trace.append({"target": f"item:{code}", "property": "requiredness", "value": flag,
                          "because": ["REQUIRE_ITEM" if forced else "MAKE_OPTIONAL"]})
        result.required = flag and result.visible
        result.answer = states[code]
        if not result.visible:
            result.comment_required, result.comment_prompts, result.evidence, result.findings = False, [], None, []

    # Declared-N/A is not trusted: a required, visible question cannot be waved through by the client.
    for code in sorted(na_codes & set(results)):
        result = results[code]
        if result.visible and result.required:
            result.answer = AnswerState(INVALID, errors=({"code": "not_applicable_not_allowed",
                                                          "message": "This question is required and cannot be marked not applicable."},))
    unanswered = sorted(c for c, r in results.items() if r.visible and r.required and r.answer.status == EMPTY)
    invalid = sorted(c for c, r in results.items() if r.visible and r.answer.status == INVALID)
    completion = {
        "applicable_required": sum(1 for r in results.values() if r.visible and r.required),
        "unanswered_required": unanswered, "invalid": invalid,
        "complete": not unanswered and not invalid and not any(e["code"] in ("cycle",) for e in errors),
    }
    rule_list = [
        {"code": r.code, "name": r.name, "enabled": r.is_active,
         "matched": rule_results[r.code].value if r.is_active and r.code in rule_results else None}
        for r in compiled.rules
    ]
    sections = {code: {"visible": visible} for code, visible in section_visible.items()}
    return Evaluation(results, sections, rule_list, messages, errors, completion, trace)


def evaluate_snapshot(snapshot: VersionSnapshot, answers: Mapping[str, Any], not_applicable: Iterable[str] = ()) -> Evaluation:
    """Convenience: compile and evaluate in one call (compile once and reuse for repeated evaluations)."""
    return evaluate(compile_rules(snapshot), answers, not_applicable)


__all__ = ["CompiledRuleSet", "Evaluation", "ItemResult", "compile_rules", "describe_cycle", "evaluate", "evaluate_snapshot"]
