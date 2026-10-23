"""Publication validation of a workflow version: blocking **errors** and advisory **warnings** (ADR 0070).

A version with any error cannot be published.  Checks: exactly one initial state; at least one terminal state of each outcome reachable
(a success one is required); every state reachable from the initial one; no dead ends (a non-terminal state with no enabled way out) and no
exits from terminal states; transitions connect states of the version; unique branches; conditions valid against the target registry;
handlers registered for the target type; assignee policies, approval modes and separation options coherent; cycles only when the version
allows loops; every conditional branch group has a defined fallback (otherwise an unmatched condition fails closed - a warning).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from . import assignment, conditions, handlers
from .constants import MAX_STATES, MAX_TRANSITIONS, ApprovalMode, StateKind
from .targets import TargetNotFound, get_spec


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


def validate(version: Any) -> Report:
    report = Report()
    definition = version.definition
    try:
        get_spec(definition.target_type)
    except TargetNotFound:
        report.errors.append(f"The target type “{definition.target_type}” is not registered.")
        return report
    states = list(version.states.all().order_by("sort_order", "code"))
    transitions = list(version.transitions.select_related("source", "target").order_by("source__sort_order", "sort_order", "action"))
    if not states:
        report.errors.append("The workflow has no states.")
        return report
    if len(states) > MAX_STATES or len(transitions) > MAX_TRANSITIONS:
        report.errors.append(f"A workflow may have at most {MAX_STATES} states and {MAX_TRANSITIONS} transitions.")
    by_code = {s.code: s for s in states}
    initial = [s for s in states if s.kind == StateKind.INITIAL]
    if len(initial) != 1:
        report.errors.append("Exactly one state must be the initial state." + (f" There are {len(initial)}." if initial else ""))
    enabled = [t for t in transitions if t.enabled]
    out: dict[str, list[Any]] = defaultdict(list)
    for t in enabled:
        out[t.source.code].append(t)
    terminals = [s for s in states if s.is_terminal]
    if not any(s.kind == StateKind.SUCCESS for s in terminals):
        report.errors.append("There must be at least one state that completes the workflow successfully.")
    for s in states:
        if s.is_terminal and out.get(s.code):
            report.errors.append(f"“{s.name}” ends the workflow and cannot have transitions leaving it.")
        if not s.is_terminal and not out.get(s.code):
            report.errors.append(f"“{s.name}” is a dead end: it has no enabled transition and is not a terminal state.")
    # reachability
    if len(initial) == 1:
        seen, stack = {initial[0].code}, [initial[0].code]
        while stack:
            for t in out.get(stack.pop(), []):
                if t.target.code not in seen:
                    seen.add(t.target.code)
                    stack.append(t.target.code)
        for s in states:
            if s.code not in seen:
                report.errors.append(f"“{s.name}” cannot be reached from the initial state.")
        if not any(s.kind == StateKind.SUCCESS and s.code in seen for s in states):
            report.errors.append("No successful end state can be reached from the initial state.")
        if not any(s.kind == StateKind.FAILURE for s in states):
            report.warnings.append("There is no unsuccessful end state; a rejection cannot end the workflow.")
        # cycles
        if not version.allow_loops and _has_cycle(by_code, out):
            report.errors.append("The workflow contains a loop. Allow loops explicitly on the version if a return-for-correction cycle is intended.")
    # branches and transitions
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for t in enabled:
        groups[(t.source.code, t.action)].append(t)
    for (src, action), items in groups.items():
        unconditional = [t for t in items if not t.condition]
        if len(unconditional) > 1:
            report.errors.append(f"“{action}” from “{src}” has more than one unconditional branch.")
        elif unconditional and items.index(unconditional[0]) != len(items) - 1:
            report.errors.append(f"The unconditional branch of “{action}” from “{src}” must come last (a later branch could never be reached).")
        elif not unconditional and len(items) >= 1 and any(t.condition for t in items):
            report.warnings.append(f"“{action}” from “{src}” has no fallback branch: when no condition matches, the action is refused (fail closed).")
    registered = {h.code for h in handlers.for_target(definition.target_type)}
    for t in transitions:
        label = f"“{t.action}” from “{t.source.code}”"
        if not t.action or not t.label.strip():
            report.errors.append(f"A transition from “{t.source.code}” needs an action code and a label.")
        if t.handler and t.handler not in registered:
            report.errors.append(f"{label}: “{t.handler}” is not a registered business action for {definition.target_type} records.")
        _, problems = conditions.clean_condition(definition.target_type, t.condition, f"{label} condition")
        report.errors.extend(problems)
        if t.decision == "approve" and t.source.is_terminal:
            report.errors.append(f"{label}: an approval cannot leave a terminal state.")
    _, problems = conditions.clean_condition(definition.target_type, version.start_condition, "start condition")
    report.errors.extend(problems)
    # states: policies
    for s in states:
        policy, problems = assignment.clean_policy(s.assignee_policy, definition.organization)
        report.errors.extend(f"“{s.name}”: {p}" for p in problems)
        _, sep_problems = assignment.clean_separation(s.separation)
        report.errors.extend(f"“{s.name}”: {p}" for p in sep_problems)
        decides = [t for t in out.get(s.code, []) if t.actor_rule == "assignee"]
        if decides and policy is None:
            report.errors.append(f"“{s.name}” has transitions for assignees but no assignee policy: nobody would ever receive a task.")
        if policy is not None and not decides:
            report.warnings.append(f"“{s.name}” creates tasks but none of its transitions is for an assignee.")
        if s.approval_mode == ApprovalMode.ALL and policy is not None and policy["type"] != "user":
            report.errors.append(f"“{s.name}”: requiring every approver needs a policy that names specific users.")
        if s.approval_mode == ApprovalMode.MIN:
            if s.min_approvals < 2:
                report.warnings.append(f"“{s.name}”: a minimum of one approval is the same as “one approval”.")
            if policy is not None and policy["type"] == "user" and s.min_approvals > len(policy["users"]):
                report.errors.append(f"“{s.name}”: {s.min_approvals} approvals are required but only {len(policy['users'])} users are named.")
        if s.approval_mode != ApprovalMode.ONE and not any(t.decision == "approve" for t in out.get(s.code, [])):
            report.warnings.append(f"“{s.name}” has an approval mode but no transition marked as an approval.")
        if s.due_hours is not None and s.due_hours > 24 * 365:
            report.errors.append(f"“{s.name}”: the deadline is longer than a year.")
    return report


def _has_cycle(by_code: dict[str, Any], out: dict[str, list[Any]]) -> bool:
    colour: dict[str, int] = {}

    def visit(code: str) -> bool:
        colour[code] = 1
        for t in out.get(code, []):
            nxt = t.target.code
            if colour.get(nxt) == 1 or (nxt not in colour and visit(nxt)):
                return True
        colour[code] = 2
        return False

    return any(code not in colour and visit(code) for code in by_code)
