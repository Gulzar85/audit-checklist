"""Validation of a whole rule set: references, cycles, definite conflicts and limits.

``validate_rule_set`` is the single gate used when a rule is created or edited, when a version is published, when rules
are imported and by the API's ``validate`` endpoint - so a rule set that passes here is safe to evaluate.

Findings are ``error`` (blocks saving/publishing) or ``warning`` (informational):

* errors - unknown/foreign references, operators not valid for the source type, malformed values or actions,
  limit breaches, duplicate rule codes, dependency cycles, and *definite* conflicts (two enabled rules with the same
  condition asking for opposite things of the same target);
* warnings - *possible* conflicts (different conditions that can both hold; the documented precedence will apply),
  rules that read or target an inactive question.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from .compiled import CompiledRule
from .constraints import tighten
from .errors import Issue
from .graph import build_graph, describe_cycle, find_cycle
from .limits import limit
from .schema import compile_rule
from .snapshot import VersionSnapshot


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)
    compiled: list[CompiledRule] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"valid": self.ok, "errors": [i.as_dict() for i in self.errors], "warnings": [i.as_dict() for i in self.warnings]}


_OPPOSITES = {("SHOW_ITEM", "HIDE_ITEM"), ("SHOW_SECTION", "HIDE_SECTION"), ("REQUIRE_ITEM", "MAKE_OPTIONAL")}


def _signature(rule: CompiledRule) -> str:
    return json.dumps(rule.stored_condition, sort_keys=True)


def validate_rule_set(snapshot: VersionSnapshot) -> ValidationReport:
    report = ValidationReport()
    rules = list(snapshot.rules)
    if len(rules) > limit("max_rules_per_version"):
        report.issues.append(Issue("error", "too_many", f"A version may have at most {limit('max_rules_per_version')} rules."))
    seen: set[str] = set()
    for spec in rules:
        if spec.code in seen:
            report.issues.append(Issue("error", "duplicate_code", f"Rule code “{spec.code}” is used twice.", rule=spec.code))
        seen.add(spec.code)
        compiled, issues = compile_rule(snapshot, spec)
        report.issues.extend(issues)
        if compiled is not None:
            report.compiled.append(compiled)
    active = [r for r in report.compiled if r.is_active]
    _warn_inactive(snapshot, active, report)
    _conflicts(snapshot, active, report)
    if report.compiled:
        cycle = find_cycle(build_graph(snapshot, active))
        if cycle:
            rules_in_cycle = [rule for _, rule in cycle if rule]
            report.issues.append(Issue("error", "cycle", describe_cycle(snapshot, cycle), rule=rules_in_cycle[0] if rules_in_cycle else ""))
    return report


def _warn_inactive(snapshot: VersionSnapshot, active: list[CompiledRule], report: ValidationReport) -> None:
    for rule in active:
        for source in sorted(rule.sources()):
            if not snapshot.items[source].active:
                report.issues.append(Issue("warning", "inactive_source", f"Rule {rule.code} reads {source}, which is inactive: the condition can never match.", rule=rule.code))
        for action in rule.actions:
            if action.target_kind == "item" and not snapshot.items[action.target_code].active:
                report.issues.append(Issue("warning", "inactive_target", f"Rule {rule.code} targets {action.target_code}, which is inactive.", rule=rule.code))


def _conflicts(snapshot: VersionSnapshot, active: list[CompiledRule], report: ValidationReport) -> None:
    by_target: dict[tuple[str, str], list[tuple[CompiledRule, str]]] = defaultdict(list)
    for rule in active:
        for action in rule.actions:
            if action.target_kind:
                by_target[(action.target_kind, action.target_code)].append((rule, action.type))
    for (kind, code), entries in by_target.items():
        for first, second in _OPPOSITES:
            a = [r for r, t in entries if t == first]
            b = [r for r, t in entries if t == second]
            for ra in a:
                for rb in b:
                    if ra.code == rb.code:
                        report.issues.append(Issue("error", "conflict", f"Rule {ra.code} both {first} and {second} on {code}.", rule=ra.code))
                    elif _signature(ra) == _signature(rb):
                        report.issues.append(Issue(
                            "error", "conflict",
                            f"Rules {ra.code} and {rb.code} have the same condition but ask for opposite things of {code} ({first} / {second}).",
                            rule=ra.code))
                    else:
                        winner = "hiding" if first.startswith("SHOW") else "requiring"
                        report.issues.append(Issue(
                            "warning", "possible_conflict",
                            f"Rules {ra.code} and {rb.code} can both apply to {code}; when they do, {winner} wins.", rule=ra.code))
        constraint_rules = [r for r, t in entries if t == "SET_VALIDATION_CONSTRAINT"]
        if len(constraint_rules) > 1 and kind == "item":
            item = snapshot.items[code]
            sets = [next(a.params["constraints"] for a in r.actions if a.type == "SET_VALIDATION_CONSTRAINT" and a.target_code == code) for r in constraint_rules]
            _config, conflict = tighten(item, sets)
            if conflict:
                same = len({_signature(r) for r in constraint_rules}) == 1
                report.issues.append(Issue("error" if same else "warning", "conflict" if same else "possible_conflict",
                                           f"If rules {', '.join(r.code for r in constraint_rules)} all apply: {conflict}.", rule=constraint_rules[0].code))
