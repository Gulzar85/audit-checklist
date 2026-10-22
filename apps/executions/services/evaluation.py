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
    #: ``{item id: [live EvidenceAttachment]}`` (every status; only AVAILABLE ones count as the answer).
    evidence: dict[str, list[Any]] = field(default_factory=dict)

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


MEDIA_TYPES = ("photo", "file", "signature")


def evidence_answer(answer_type: str, attachments: list[Any]) -> Any:
    """The Phase 5 answer value of a media item, derived from its **available** attachments (server-validated files only)."""
    ready = [a for a in attachments if a.asset.status == "available"]
    if not ready:
        return None
    if answer_type == "signature":
        return {"signer_name": ready[0].caption or "", "file_id": str(ready[0].asset_id)}
    return [{"file_id": str(a.asset_id), "name": a.asset.original_name, "size": a.asset.size, "content_type": a.asset.content_type} for a in ready]


def build_state(execution: ChecklistExecution, *, responses: dict[str, ChecklistResponse] | None = None,
                loaded: tuple[VersionSnapshot, CompiledRuleSet] | None = None) -> ExecutionState:
    from apps.evidence import selectors as evidence_selectors

    snapshot, compiled = loaded or compile_for(execution)
    responses = load_responses(execution) if responses is None else responses
    answers, na = answers_from(responses)
    evidence = evidence_selectors.by_item(execution)
    for code, spec in snapshot.items.items():  # media answers are not typed in: they ARE the attached, verified files
        if spec.answer_type in MEDIA_TYPES:
            value = evidence_answer(spec.answer_type, evidence.get(spec.id, []))
            if value is not None:
                answers[code] = value
    evaluation = evaluate(compiled, answers, na)
    return ExecutionState(execution, snapshot, compiled, responses, evaluation, {i.id: i for i in snapshot.items.values()}, dict(evidence))
