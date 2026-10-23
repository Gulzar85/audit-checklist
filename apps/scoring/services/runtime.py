"""Scoring executions: the live preview, the final snapshot at submission, controlled recalculation and the designer's what-if.

Every number comes from ``engine.calculate`` - the single authoritative calculation.  The inputs are the *server-side* evaluated state
of the execution (stored responses, normalized by the Phase 5 validators, evaluated by the Phase 6 rules engine - which also decides what
is hidden or N/A).  A client-supplied score, grade or total is never read anywhere.

Policies (ADR 0075):

* An execution that is **in progress** has no stored score: its score is computed on demand and is always *provisional*.
* **Submission** writes the one final, immutable ``ScoreResult`` inside the submission transaction.  A failure there is audited and
  never loses the submission; ``score_executions`` (management command) completes it later.
* A final result is never overwritten.  An authorized recalculation (``scoring.finalize_recalculate`` + a reason) computes again under
  the version's frozen configuration; if nothing differs it changes nothing, otherwise it appends a new current result and retires the
  previous one (kept).  Offline answers go through the same ``save_answers`` / ``submit`` services, so they are scored by this code too.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from apps.checklists.rules import evaluate_snapshot, snapshot_for_version
from apps.core.services import log_activity
from apps.executions.constants import ExecutionStatus
from apps.executions.models import ChecklistExecution
from apps.executions.services import build_state
from apps.executions.services.base import lock_execution

from .. import config as cfg
from .. import constants as k
from .. import policies, selectors
from ..engine import ItemFacts, ScoreOutcome, calculate, input_fingerprint
from ..models import ScoreResult, ScoringConfig
from .errors import ScoringConflict, ScoringError

logger = logging.getLogger(__name__)

VERBS = {"final": "scoring.score_finalized", "recalculated": "scoring.score_recalculated", "failed": "scoring.score_failed",
         "unchanged": "scoring.recalculation_unchanged", "previewed": "scoring.config_previewed"}


def facts_from(snapshot: Any, evaluation: Any) -> list[ItemFacts]:
    """The engine's input for every question of the version, from an evaluated state (the Phase 6 answer states)."""
    out = []
    for code, spec in snapshot.items.items():
        result = evaluation.items.get(code)
        status = result.answer.status if result is not None else "empty"
        out.append(ItemFacts(
            code=code, section=spec.section_code, question=spec.question, answer_type=spec.answer_type, type_config=spec.config,
            visible=bool(result.visible) if result is not None else True, status=status,
            value=result.answer.serialized if (result is not None and status == "answered") else None,
            labels={o.value: o.label for o in spec.options},
        ))
    return out


def _config_for(version: Any) -> ScoringConfig | None:
    row = selectors.get_config(version)
    return row if (row is not None and row.enabled) else None


def _outcome(row: ScoringConfig, snapshot: Any, evaluation: Any, *, final: bool) -> tuple[ScoreOutcome, str]:
    data = cfg.parse(row.settings, row.document)
    facts = facts_from(snapshot, evaluation)
    outcome = calculate(data, facts, section_names={code: s.name for code, s in snapshot.sections.items()}, final=final)
    return outcome, input_fingerprint([f for f in facts if f.code in data.items])


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def score_of(execution: ChecklistExecution) -> dict[str, Any]:
    """The score view of an execution: the stored final result when there is one, else a live provisional computation.

    ``{"applicable": False}`` when the version's scoring is absent or disabled - never an invented zero.
    """
    row = selectors.get_config(execution.version)
    if row is None or not row.enabled:
        return {"applicable": False, "reason": "disabled" if row is not None else "not_configured"}
    stored = selectors.current_result(execution)
    if stored is not None:
        return {"applicable": True, "stored": True, "result": stored, "outcome": None}
    if execution.status != ExecutionStatus.IN_PROGRESS:
        return {"applicable": True, "stored": False, "result": None, "outcome": None, "pending": True}
    state = build_state(execution)
    outcome, _fingerprint = _outcome(row, state.snapshot, state.evaluation, final=False)
    return {"applicable": True, "stored": False, "result": None, "outcome": outcome}


def preview_sample(version: Any, *, settings: dict[str, Any] | None, document: dict[str, Any] | None, answers: dict[str, Any],
                   not_applicable: list[str] | None = None, final: bool = True) -> dict[str, Any]:
    """What-if for designers: score synthetic answers under the saved draft (or a candidate configuration).  Nothing is stored."""
    from .configuration import _clean_settings, _settings_of  # noqa: PLC0415

    row = selectors.get_config(version)
    merged = _clean_settings(settings or {}, _settings_of(row))
    doc = document if document is not None else (row.document if row else cfg.empty_document())
    snapshot = snapshot_for_version(version)
    issues = cfg.validate(merged, doc, snapshot)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        return {"valid": False, "issues": [i.as_dict() for i in issues], "outcome": None}
    if not merged["enabled"]:
        return {"valid": True, "issues": [i.as_dict() for i in issues], "outcome": None, "disabled": True}
    unknown = [code for code in list(answers) + list(not_applicable or []) if code not in snapshot.items]
    if unknown:
        raise ScoringError(f"Unknown questions in the sample answers: {', '.join(sorted(set(unknown))[:10])}.", "unknown_item")
    evaluation = evaluate_snapshot(snapshot, answers, not_applicable or [])
    data = cfg.parse(merged, doc)
    facts = facts_from(snapshot, evaluation)
    outcome = calculate(data, facts, section_names={code: s.name for code, s in snapshot.sections.items()}, final=final)
    return {"valid": True, "issues": [i.as_dict() for i in issues], "outcome": outcome}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def _store(row: ScoringConfig, execution: ChecklistExecution, outcome: ScoreOutcome, fingerprint: str, *, kind: str, actor: Any, reason: str = "") -> ScoreResult:
    return ScoreResult.objects.create(
        organization_id=execution.organization_id, execution=execution, version_id=execution.version_id, config=row, config_hash=row.content_hash,
        algorithm=outcome.algorithm, status=outcome.status, kind=kind, is_current=True, method=outcome.method, earned=outcome.earned,
        available=outcome.available, percentage=outcome.percentage, grade=outcome.grade, label=outcome.label, display_label=outcome.display_label,
        critical=outcome.critical, breakdown=outcome.breakdown(), execution_revision=execution.revision, input_fingerprint=fingerprint, reason=reason[:500],
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )


def _audit(verb: str, execution: ChecklistExecution, actor: Any, description: str, **meta: Any) -> None:
    log_activity(verb=verb, organization=execution.organization, actor=actor, target=execution, description=description,
                 metadata={"execution_id": str(execution.pk), "assignment_id": str(execution.assignment_id), "reference": execution.reference, **meta})


def finalize_after_submission(execution: ChecklistExecution, actor: Any, state: Any) -> ScoreResult | None:
    """Called inside the submission transaction (after the execution became submitted).  Idempotent; never raises into the submission."""
    row = _config_for(execution.version)
    if row is None:
        return None
    try:
        with transaction.atomic():
            existing = selectors.current_result(execution)
            if existing is not None:
                return existing
            outcome, fingerprint = _outcome(row, state.snapshot, state.evaluation, final=True)
            result = _store(row, execution, outcome, fingerprint, kind=k.ResultKind.SUBMISSION, actor=actor)
            _audit(VERBS["final"], execution, actor, f"Score of {execution.reference} calculated", status=result.status,
                   percentage=str(result.percentage) if result.percentage is not None else None, grade=result.grade, critical=result.critical,
                   config_hash=result.config_hash, algorithm=result.algorithm)
            return result
    except Exception as exc:  # noqa: BLE001 - a scoring defect must never lose a submitted checklist; it is audited and retried
        logger.exception("Scoring failed for execution %s", execution.pk)
        _audit(VERBS["failed"], execution, actor, f"Scoring of {execution.reference} failed", error=type(exc).__name__)
        return None


def score_pending(execution: ChecklistExecution, *, actor: Any = None) -> ScoreResult | None:
    """Retry for a submitted execution whose submission-time scoring failed (used by ``score_executions``).  Idempotent."""
    execution = ChecklistExecution.objects.select_related("organization", "assignment", "version__template").get(pk=execution.pk)
    if execution.status != ExecutionStatus.SUBMITTED:
        return None
    return finalize_after_submission(execution, actor or execution.submitted_by, build_state(execution))


@transaction.atomic
def recalculate(execution: ChecklistExecution, *, actor: Any, reason: str) -> tuple[ScoreResult, bool]:
    """Authorized recalculation of a *final* score.  Returns ``(current result, changed)``; ``changed`` False means identical inputs
    under the same configuration and algorithm, so nothing was appended."""
    reason = (reason or "").strip()
    if not policies.can_finalize_recalculate(actor, execution):
        raise PermissionDenied
    if not reason:
        raise ScoringError("A reason is required to recalculate a final score.", "reason_required")
    locked = lock_execution(execution.pk)
    if locked.status != ExecutionStatus.SUBMITTED:
        raise ScoringConflict("Only the score of a submitted execution can be recalculated; an execution in progress is previewed instead.", "not_submitted")
    row = _config_for(locked.version)
    if row is None:
        raise ScoringError("Scoring is not enabled for this checklist version.", "not_scored")
    state = build_state(locked)
    outcome, fingerprint = _outcome(row, state.snapshot, state.evaluation, final=True)
    current = selectors.current_result(locked)
    if current is not None and (current.input_fingerprint, current.config_hash, current.algorithm) == (fingerprint, row.content_hash, outcome.algorithm):
        _audit(VERBS["unchanged"], locked, actor, f"Recalculation of {locked.reference} changed nothing", reason=reason[:200])
        return current, False
    if current is not None:
        ScoreResult.objects.filter(pk=current.pk).update(is_current=False)
    try:
        with transaction.atomic():
            result = _store(row, locked, outcome, fingerprint, kind=k.ResultKind.RECALCULATION, actor=actor, reason=reason)
    except IntegrityError as exc:  # a concurrent recalculation won the race for the single current result
        raise ScoringConflict("The score was recalculated at the same time; reload and try again.", "concurrent_recalculation") from exc
    _audit(VERBS["recalculated"], locked, actor, f"Score of {locked.reference} recalculated", reason=reason[:200], status=result.status,
           percentage=str(result.percentage) if result.percentage is not None else None, previous=str(current.pk) if current else None)
    return result, True
