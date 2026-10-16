"""The conditional rules engine (Phase 6).

Declarative rules - a condition tree and a list of actions, referencing questions and sections by code - decide which
questions are visible and required, which options and limits apply, and which messages, comment and evidence
requirements are active.  Validation (references, operators, values, cycles, conflicts) and evaluation share one
implementation used by the builder, the API and (later) execution, and reuse the Phase 5 answer-type engine for every
comparison.  Nothing is executed and nothing is persisted here.

    from apps.checklists.rules import snapshot_for_version, compile_rules, evaluate, validate_rule_set
"""

from __future__ import annotations

from .actions import ACTIONS, describe_all as describe_actions
from .describe import describe_rule
from .engine import CompiledRuleSet, Evaluation, compile_rules, evaluate, evaluate_snapshot
from .errors import Issue
from .limits import all_limits, limit
from .operators import OPERATORS, describe_all as describe_operators, operators_for
from .snapshot import ItemSpec, RuleSpec, VersionSnapshot, snapshot_for_version
from .tri import Tri
from .validation import ValidationReport, validate_rule_set

__all__ = [
    "ACTIONS", "OPERATORS", "CompiledRuleSet", "Evaluation", "ItemSpec", "Issue", "RuleSpec", "Tri", "ValidationReport",
    "VersionSnapshot", "all_limits", "compile_rules", "describe_actions", "describe_operators", "describe_rule", "evaluate",
    "evaluate_snapshot", "limit", "operators_for", "snapshot_for_version", "validate_rule_set",
]
