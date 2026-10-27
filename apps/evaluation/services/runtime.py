"""Evaluating executions: the live provisional evaluation, the final stored run, authorized re-evaluation and the designer's what-if.

Every outcome comes from ``engine.evaluate`` over *server-side* inputs: the stored responses (Phase 5 values, Phase 6 answer states), the **stored**
Phase 15 score (never recalculated here) and execution metadata.  A client-supplied result is never read anywhere.

Policies (ADR 0077):

* An execution **in progress** has no stored evaluation: it is computed on read, is *provisional*, reads the score as unavailable, and **never runs actions**.
* **Submission** records the one *final* run (after the score snapshot, inside the submission transaction) and then runs the requested actions, each in its own
  savepoint.  A defect is audited and never loses the submission; ``evaluate_executions`` completes it (idempotent).
* **Idempotency:** the run key is ``sha256(execution, rule-set hash, engine version, inputs fingerprint, execution revision)``.  The same inputs under the same
  rules return the existing run (no new row, no side effect); changed inputs, a changed score or a different rule-set version append a new current run
  and retire the previous one (kept).  An action key (``eval:<execution>:<rule>:<index>``) that already succeeded is *skipped*, so a finding or workflow is never
  requested twice for the same rule and execution.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.checklists.rules import evaluate_snapshot, snapshot_for_version
from apps.core.services import log_activity
from apps.executions.constants import ExecutionStatus
from apps.executions.models import ChecklistExecution
from apps.executions.services import build_state
from apps.executions.services.base import lock_execution
from apps.scoring import selectors as scoring_selectors

from .. import actions as action_registry
from .. import events as evaluation_events
from .. import config as cfg
from .. import constants as k
from .. import policies, selectors, sources
from ..engine import Evaluation, Inputs, evaluate, input_fingerprint
from ..models import EvaluationActionRequest, EvaluationRun
from .errors import EvaluationConflict, EvaluationError

logger = logging.getLogger(__name__)

VERBS = {"recorded": "evaluation.run_recorded", "unchanged": "evaluation.run_unchanged", "failed": "evaluation.run_failed",
         "action": "evaluation.action_{status}", "previewed": "evaluation.config_previewed"}


def inputs_from(snapshot: Any, evaluation: Any, execution: Any, score_result: Any | None) -> Inputs:
    """The normalized facts of a run, read once: every question's Phase 6 answer state, the stored score, the execution metadata."""
    answers = {code: (evaluation.items[code].answer if code in evaluation.items else sources.AnswerState(sources.INVALID)) for code in snapshot.items}
    return Inputs(answers=answers, score=sources.score_states(score_result), execution=sources.execution_states(execution))


def _compile(version: Any, row: Any, snapshot: Any) -> list[cfg.CompiledRule]:
    issues, compiled, _doc = cfg.validate_document(row.enabled, row.document, snapshot, None)  # run time: structure only (reference data was checked at publication)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        raise EvaluationError("The evaluation rules of this version are invalid: " + "; ".join(i.message for i in errors[:3]), "invalid_ruleset")
    return compiled


def _key(execution: Any, row: Any, fingerprint: str, score_result: Any) -> str:
    text = "|".join([str(execution.pk), row.content_hash, str(k.ENGINE_VERSION), fingerprint, str(execution.revision), str(score_result.pk) if score_result else "-"])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _audit(verb: str, execution: ChecklistExecution, actor: Any, description: str, **meta: Any) -> None:
    log_activity(verb=verb, organization=execution.organization, actor=actor if getattr(actor, "is_authenticated", False) else None, target=execution,
                 description=description, metadata={"execution_id": str(execution.pk), "assignment_id": str(execution.assignment_id), "reference": execution.reference, **meta})


# ---------------------------------------------------------------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------------------------------------------------------------
def evaluation_of(execution: ChecklistExecution) -> dict[str, Any]:
    """The evaluation view of an execution: the stored current run, or a provisional live computation while in progress.

    ``{"applicable": False, "reason"}`` when the version has no (enabled) rule set - never an invented PASS.
    """
    row = selectors.get_ruleset(execution.version)
    if row is None or not row.enabled:
        return {"applicable": False, "reason": "disabled" if row is not None else "not_configured"}
    stored = selectors.current_run(execution)
    if stored is not None:
        return {"applicable": True, "stored": True, "run": stored, "evaluation": None}
    if execution.status != ExecutionStatus.IN_PROGRESS:
        return {"applicable": True, "stored": False, "run": None, "evaluation": None, "pending": True}
    state = build_state(execution)
    compiled = _compile(execution.version, row, state.snapshot)
    return {"applicable": True, "stored": False, "run": None, "evaluation": evaluate(compiled, inputs_from(state.snapshot, state.evaluation, execution, None)), "provisional": True}


def preview_sample(version: Any, *, enabled: bool | None, document: Any, answers: dict[str, Any], not_applicable: list[str] | None, score: dict[str, Any] | None,
                   execution_status: str = "submitted") -> dict[str, Any]:
    """What-if for designers: evaluate synthetic answers and score facts under the saved draft (or a candidate).  Nothing is stored, no action runs."""
    from .configuration import validate_config

    issues, compiled, _doc = validate_config(version, enabled=enabled, document=document)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        return {"valid": False, "issues": [i.as_dict() for i in issues], "evaluation": None, "non_production": True}
    snapshot = snapshot_for_version(version)
    unknown = [c for c in list(answers) + list(not_applicable or []) if c not in snapshot.items]
    if unknown:
        raise EvaluationError(f"Unknown questions in the sample answers: {', '.join(sorted(set(unknown))[:10])}.", "unknown_item")
    if execution_status not in sources.EXECUTION_STATUSES:
        raise EvaluationError("Unknown execution status.", "invalid_status")
    try:
        score_states = sources.synthetic_score_states(score or {"status": "unavailable"})
    except sources.OperandError as exc:
        raise EvaluationError(str(exc), "invalid_score") from exc
    evaluation = evaluate_snapshot(snapshot, answers, not_applicable or [])
    facts = Inputs(answers={c: evaluation.items[c].answer for c in snapshot.items}, score=score_states, execution={"status": sources.AnswerState(sources.ANSWERED, execution_status, execution_status)})
    return {"valid": True, "issues": [i.as_dict() for i in issues], "evaluation": evaluate(compiled, facts), "non_production": True}


# ---------------------------------------------------------------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------------------------------------------------------------
def _record(execution: ChecklistExecution, row: Any, evaluation: Evaluation, fingerprint: str, key: str, score_result: Any, *, trigger: str, actor: Any,
            reason: str) -> EvaluationRun:
    return EvaluationRun.objects.create(
        organization_id=execution.organization_id, execution=execution, version_id=execution.version_id, ruleset=row, ruleset_hash=row.content_hash,
        engine_version=evaluation.engine_version, trigger=trigger, outcome=evaluation.outcome, is_current=True, execution_revision=execution.revision,
        score_result=score_result, input_fingerprint=fingerprint, idempotency_key=key, reason=reason[:500], detail=evaluation.as_dict(),
        created_by=actor if getattr(actor, "is_authenticated", False) else None)


def _run_actions(execution: ChecklistExecution, run: EvaluationRun, evaluation: Evaluation) -> list[EvaluationActionRequest]:
    done: list[EvaluationActionRequest] = []
    for requested in evaluation.actions:
        rule_code, index, action = requested["rule"], requested["index"], requested["action"]
        key = f"eval:{execution.pk}:{rule_code}:{index}"
        request = EvaluationActionRequest.objects.create(
            organization_id=execution.organization_id, run=run, execution=execution, rule_code=rule_code, action_index=index, action_type=action["type"],
            action_key=key, params=action)
        earlier = EvaluationActionRequest.objects.filter(organization_id=execution.organization_id, action_key=key, status=k.ActionStatus.SUCCEEDED).exclude(pk=request.pk).first()
        if earlier is not None:
            outcome = action_registry.ActionOutcome(k.ActionStatus.SKIPPED, earlier.result_type, earlier.result_id, "Already performed by an earlier evaluation run")
        else:
            try:
                with transaction.atomic():  # a savepoint: a failing action never poisons the evaluation or its siblings
                    outcome = action_registry.run_action(execution, action, {"rule_code": rule_code, "rule_name": requested["rule_name"], "action_key": key})
            except Exception as exc:  # noqa: BLE001 - recorded as that action's failure
                logger.exception("Evaluation action %s failed for execution %s", key, execution.pk)
                outcome = action_registry.ActionOutcome(k.ActionStatus.FAILED, message=f"{type(exc).__name__}: an unexpected error occurred")
        request.status, request.result_type, request.result_id = outcome.status, outcome.result_type, outcome.result_id
        request.message = outcome.message[:500] if outcome.status != k.ActionStatus.FAILED else ""
        request.error = outcome.message[:500] if outcome.status == k.ActionStatus.FAILED else ""
        request.finished_at = timezone.now()
        request.save(update_fields=["status", "result_type", "result_id", "message", "error", "finished_at"])
        _audit(VERBS["action"].format(status=outcome.status), execution, None, f"Action {action['type']} of rule {rule_code} {outcome.status}", rule=rule_code, action=action["type"],
               status=outcome.status, result_type=outcome.result_type, result_id=outcome.result_id)
        done.append(request)
    return done


def _evaluate(execution: ChecklistExecution, state: Any, *, trigger: str, actor: Any, reason: str = "") -> tuple[EvaluationRun | None, bool]:
    """Core (the caller holds the execution lock and is inside a transaction).  Returns ``(current run, created)``."""
    row = selectors.get_ruleset(execution.version)
    if row is None or not row.enabled:
        return None, False
    compiled = _compile(execution.version, row, state.snapshot)
    score_result = scoring_selectors.current_result(execution)
    facts = inputs_from(state.snapshot, state.evaluation, execution, score_result)
    evaluation = evaluate(compiled, facts)
    fingerprint = input_fingerprint(facts)
    key = _key(execution, row, fingerprint, score_result)
    existing = EvaluationRun.objects.filter(idempotency_key=key).first()
    if existing is not None:
        _audit(VERBS["unchanged"], execution, actor, f"Evaluation of {execution.reference} changed nothing", trigger=trigger, reason=reason[:200])
        return existing, False
    previous = selectors.current_run(execution)
    if previous is not None:
        EvaluationRun.objects.filter(pk=previous.pk).update(is_current=False)
    try:
        with transaction.atomic():
            run = _record(execution, row, evaluation, fingerprint, key, score_result, trigger=trigger, actor=actor, reason=reason)
    except IntegrityError as exc:  # a concurrent identical evaluation won the race
        raced = EvaluationRun.objects.filter(idempotency_key=key).first()
        if raced is None:
            raise EvaluationConflict("The execution was evaluated at the same time; reload and try again.", "concurrent_evaluation") from exc
        return raced, False
    requests = _run_actions(execution, run, evaluation)
    _audit(VERBS["recorded"], execution, actor, f"{execution.reference} evaluated: {run.outcome}", outcome=run.outcome, trigger=trigger, matched=evaluation.matched,
           ruleset_hash=run.ruleset_hash, engine_version=run.engine_version, actions=[{"rule": r.rule_code, "type": r.action_type, "status": r.status} for r in requests],
           previous=str(previous.pk) if previous else None, reason=reason[:200])
    evaluation_events.send_on_commit(run, actor)
    return run, True


def evaluate_after_submission(execution: ChecklistExecution, actor: Any, state: Any) -> EvaluationRun | None:
    """Called inside the submission transaction (after the score snapshot).  Idempotent; never raises into the submission."""
    if selectors.get_ruleset(execution.version) is None:
        return None
    try:
        with transaction.atomic():
            run, _created = _evaluate(execution, state, trigger=k.Trigger.SUBMISSION, actor=None)
            return run
    except Exception as exc:  # noqa: BLE001 - a defect must never lose a submitted checklist; it is audited and retried
        logger.exception("Evaluation failed for execution %s", execution.pk)
        _audit(VERBS["failed"], execution, None, f"Evaluation of {execution.reference} failed", error=type(exc).__name__)
        return None


def evaluate_after_score_change(execution: ChecklistExecution, actor: Any, result: Any) -> None:
    """Hook after an authorized score recalculation (inside its transaction): evaluate again with the new score.  Never raises."""
    if selectors.get_ruleset(execution.version) is None or execution.status != ExecutionStatus.SUBMITTED:
        return
    try:
        with transaction.atomic():
            _evaluate(execution, build_state(execution), trigger=k.Trigger.SCORE, actor=actor, reason="The score was recalculated")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Evaluation after a score change failed for execution %s", execution.pk)
        _audit(VERBS["failed"], execution, actor, f"Evaluation of {execution.reference} after the score change failed", error=type(exc).__name__)


def evaluate_pending(execution: ChecklistExecution, *, actor: Any = None) -> EvaluationRun | None:
    """Retry for a submitted execution whose submission-time evaluation did not complete (``evaluate_executions``).  Idempotent."""
    with transaction.atomic():
        locked = lock_execution(execution.pk)
        if locked.status != ExecutionStatus.SUBMITTED:
            return None
        locked = ChecklistExecution.objects.select_related("organization", "assignment__location", "version__template").get(pk=locked.pk)
        run, _created = _evaluate(locked, build_state(locked), trigger=k.Trigger.SUBMISSION, actor=actor)
        return run


@transaction.atomic
def reevaluate(execution: ChecklistExecution, *, actor: Any, reason: str) -> tuple[EvaluationRun, bool]:
    """Authorized re-evaluation of a submitted execution (``evaluation.reevaluate`` + a reason).  Returns ``(current run, changed)``."""
    reason = (reason or "").strip()
    if not policies.can_reevaluate(actor, execution):
        raise PermissionDenied
    if not reason:
        raise EvaluationError("A reason is required to re-evaluate an execution.", "reason_required")
    locked = lock_execution(execution.pk)
    if locked.status != ExecutionStatus.SUBMITTED:
        raise EvaluationConflict("Only a submitted execution can be re-evaluated; an execution in progress is previewed instead.", "not_submitted")
    locked = ChecklistExecution.objects.select_related("organization", "assignment__location", "version__template").get(pk=locked.pk)
    if selectors.get_ruleset(locked.version) is None or not selectors.get_ruleset(locked.version).enabled:
        raise EvaluationError("Automated evaluation is not enabled for this checklist version.", "not_evaluated")
    run, created = _evaluate(locked, build_state(locked), trigger=k.Trigger.REEVALUATION, actor=actor, reason=reason)
    assert run is not None
    return run, created
