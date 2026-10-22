"""Evaluate an execution: its stored responses through the Phase 5 validators and the Phase 6 rules engine (server-authoritative).

The response rows hold the *serialized* normalized value, which re-validates unchanged (the answer contract is idempotent), so
the evaluation of a saved execution always reflects what the version's rules say about exactly those answers.  Hidden answers
(rules hid the item, or the item is inactive) are *ignored*, never deleted, until submission stamps them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from apps.checklists.rules import Evaluation, compile_rules, snapshot_for_version
from apps.checklists.rules.engine import CompiledRuleSet, evaluate
from apps.checklists.rules.snapshot import ItemSpec, VersionSnapshot

from ..constants import ResponseStatus
from ..models import ChecklistExecution, ChecklistResponse


@dataclass
class ExecutionState:
    execution: ChecklistExecution
    snapshot: VersionSnapshot
    compiled: CompiledRuleSet
    responses: dict[str, ChecklistResponse]  # by item code
    evaluation: Evaluation
    by_id: dict[str, ItemSpec] = field(default_factory=dict)

    def item_for(self, ref: Any) -> ItemSpec | None:
        return self.by_id.get(str(ref))


def load_responses(execution: ChecklistExecution) -> dict[str, ChecklistResponse]:
    rows = ChecklistResponse.objects.filter(execution=execution).select_related("item")
    return {r.item.code: r for r in rows}


def answers_from(responses: Mapping[str, ChecklistResponse]) -> tuple[dict[str, Any], set[str]]:
    """``({code: serialized value}, {codes declared N/A})`` of the stored responses (stamped-hidden rows are not answers)."""
    answers: dict[str, Any] = {}
    na: set[str] = set()
    for code, r in responses.items():
        if r.is_hidden:
            continue
        if r.status == ResponseStatus.NOT_APPLICABLE:
            na.add(code)
        else:
            answers[code] = r.value
    return answers, na


def compile_for(execution: ChecklistExecution) -> tuple[VersionSnapshot, CompiledRuleSet]:
    snapshot = snapshot_for_version(execution.version)
    return snapshot, compile_rules(snapshot)


def build_state(execution: ChecklistExecution, *, responses: dict[str, ChecklistResponse] | None = None,
                loaded: tuple[VersionSnapshot, CompiledRuleSet] | None = None) -> ExecutionState:
    snapshot, compiled = loaded or compile_for(execution)
    responses = load_responses(execution) if responses is None else responses
    answers, na = answers_from(responses)
    evaluation = evaluate(compiled, answers, na)
    return ExecutionState(execution, snapshot, compiled, responses, evaluation, {i.id: i for i in snapshot.items.values()})
