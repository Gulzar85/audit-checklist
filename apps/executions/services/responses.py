"""Saving answers (single, section or whole draft).  Authorization, validation, locking, concurrency, history and audit live here.

Concurrency (ADR 0055): every save locks the execution row, so saves, submission and cancellation of one execution are
serialised.  Each response carries a ``revision``; the client sends the revision it last saw and a mismatch is reported as a
*conflict* for that item (nothing is overwritten) - an older asynchronous save can never replace a newer answer.  A batch is
partial by design: valid, current answers are stored, invalid or stale ones are reported per item and leave stored data untouched.

Clearing an answer (an empty value) deletes the response row, comment included - a comment belongs to an answer.  A client may
not write to an item the rules currently hide (or an inactive one): such answers are *retained* if they already exist, never
created or edited, and ignored until submission stamps them hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.checklists.answer_types import registry
from apps.checklists.rules.engine import evaluate
from apps.checklists.rules.state import EMPTY, INVALID, NOT_APPLICABLE
from apps.core.services import stamp_history

from .. import policies
from ..constants import ExecutionStatus, ResponseStatus
from ..models import ChecklistExecution, ChecklistResponse
from .base import VERBS, integrity_error, lock_execution, log
from .errors import ExecutionConflict
from .evaluation import ExecutionState, answers_from, build_state, compile_for
from .validation import EVIDENCE_UNAVAILABLE, is_media, problem

MAX_COMMENT = 2000


@dataclass
class AnswerInput:
    """One submitted answer: ``item`` is the checklist item's id; ``revision`` is the response revision the client last saw."""

    item: str
    value: Any = None
    not_applicable: bool = False
    comment: str | None = None  # None = leave the stored comment unchanged
    revision: int | None = None


@dataclass
class SaveResult:
    execution: ChecklistExecution
    state: ExecutionState
    saved: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    errors: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    conflicts: dict[str, int] = field(default_factory=dict)  # item id -> the revision now stored (0 = none)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.conflicts


def _is_empty(answer_type: str, raw: Any) -> bool:
    return registry.get(answer_type).is_empty(raw) if answer_type in registry else raw in (None, "")


def require_editable(execution: ChecklistExecution, actor: Any) -> None:
    """Authorization first (403), then the state of the execution (409) - so state cannot be probed without permission."""
    if not policies.can_work_at(actor, execution):
        raise PermissionDenied
    if execution.status == ExecutionStatus.SUBMITTED:
        raise ExecutionConflict(_("This checklist was already submitted and can no longer be edited."), "already_submitted")
    if execution.status != ExecutionStatus.IN_PROGRESS:
        raise ExecutionConflict(_("This execution was cancelled and can no longer be edited."), "not_editable")


@transaction.atomic
def save_answers(execution: ChecklistExecution, *, actor: Any, inputs: list[AnswerInput]) -> SaveResult:
    """Validate and store ``inputs``; see the module docstring for the semantics.  Returns what was saved and what was refused."""
    locked = lock_execution(execution.pk)
    require_editable(locked, actor)
    loaded = compile_for(locked)
    snapshot, compiled = loaded
    state = build_state(locked, loaded=loaded)
    errors: dict[str, list[dict[str, str]]] = {}
    conflicts: dict[str, int] = {}
    candidates: dict[str, AnswerInput] = {}  # by item code

    for inp in inputs:
        spec = state.item_for(inp.item)
        key = str(inp.item)
        if spec is None:  # unknown, malformed, or another version's item: one generic answer
            errors[key] = [problem("unknown_item", "This question is not part of this checklist.")]
            continue
        existing = state.responses.get(spec.code)
        stored = existing.revision if existing else 0
        if (inp.revision or 0) != stored:
            conflicts[spec.id] = stored
            continue
        if inp.comment is not None and len(inp.comment.strip()) > MAX_COMMENT:
            errors[spec.id] = [problem("comment_too_long", f"A comment may be at most {MAX_COMMENT} characters.")]
            continue
        if is_media(spec.answer_type) and not inp.not_applicable and not _is_empty(spec.answer_type, inp.value):
            errors[spec.id] = [problem("evidence_unavailable", EVIDENCE_UNAVAILABLE)]
            continue
        candidates[spec.code] = inp

    accepted = dict(candidates)
    evaluation = state.evaluation
    for _round in range(len(candidates) + 1):  # drop what fails and re-evaluate: dependents may change when a source is refused
        answers, na = answers_from(state.responses)
        for code, inp in accepted.items():
            answers.pop(code, None)
            na.discard(code)
            if inp.not_applicable:
                na.add(code)
            elif not _is_empty(snapshot.items[code].answer_type, inp.value):
                answers[code] = inp.value
        evaluation = evaluate(compiled, answers, na)
        bad = []
        for code in accepted:
            result = evaluation.items[code]
            if not result.visible:
                errors[snapshot.items[code].id] = [problem("item_hidden", "This question does not apply right now, so it cannot be answered.")]
                bad.append(code)
            elif result.answer.status == INVALID:
                errors[snapshot.items[code].id] = [problem(e.get("code", "invalid"), e.get("message", "This answer is not valid."))
                                                   for e in result.answer.errors]
                bad.append(code)
        if not bad:
            break
        for code in bad:
            del accepted[code]

    now = timezone.now()
    saved: list[str] = []
    cleared: list[str] = []
    for code, inp in accepted.items():
        spec = snapshot.items[code]
        answer = evaluation.items[code].answer
        existing = state.responses.get(code)
        if answer.status == EMPTY:
            if existing is not None:
                existing.delete()
                cleared.append(spec.id)
            continue
        comment = (existing.comment if existing else "") if inp.comment is None else inp.comment.strip()
        fields = {"status": ResponseStatus.NOT_APPLICABLE if answer.status == NOT_APPLICABLE else ResponseStatus.ANSWERED,
                  "value": None if answer.status == NOT_APPLICABLE else answer.serialized, "comment": comment}
        if existing is not None:
            if all(getattr(existing, k) == v for k, v in fields.items()):
                continue  # nothing changed: no revision bump, no history
            for k, v in fields.items():
                setattr(existing, k, v)
            existing.revision += 1
            existing.answered_by, existing.answered_at = actor, now
            stamp_history(existing, actor, VERBS["saved"])
            existing.save()
        else:
            row =ChecklistResponse(execution=locked, item_id=spec.id, answered_by=actor, answered_at=now, **fields)
            stamp_history(row, actor, VERBS["saved"])
            try:
                with transaction.atomic():
                    row.save()
            except IntegrityError as exc:
                raise integrity_error(exc) from exc
        saved.append(spec.id)

    if saved or cleared:
        locked.revision += 1
        locked.last_activity_at = now
        locked.save(update_fields=["revision", "last_activity_at", "updated_at"])
        log(VERBS["saved"], locked, actor, f"Saved {len(saved)} answer(s) on {locked.reference}", saved=len(saved), cleared=len(cleared),
            items=(saved + cleared)[:50])
    return SaveResult(locked, build_state(locked, loaded=loaded), saved, cleared, errors, conflicts)


def save_answer(execution: ChecklistExecution, *, actor: Any, item: str, value: Any = None, not_applicable: bool = False,
                comment: str | None = None, revision: int | None = None) -> SaveResult:
    """Save one answer (the API's ``PUT .../responses/{item}/``)."""
    return save_answers(execution, actor=actor, inputs=[AnswerInput(str(item), value, not_applicable, comment, revision)])

