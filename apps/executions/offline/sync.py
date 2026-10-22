"""Synchronization of offline operations (ADR 0057): idempotent, ordered, per-operation atomic, server-authoritative.

Contract
--------
* A request carries up to ``MAX_OPERATIONS`` operations.  They are processed **in ``seq`` order**; each is its own transaction and
  yields its own result - there is no batch rollback.  A ``submit`` is **blocked** (not attempted) when an earlier operation for the
  same assignment in the batch was rejected or conflicted: submission never runs over unresolved answers.
* Statuses: ``applied`` (stored; a repeat reports ``duplicate: true``), ``conflict`` (nothing changed; carries the server's current
  state), ``rejected`` (nothing changed; a final answer for this payload), ``blocked`` (a prerequisite failed; retry later).
* Idempotency: ``(user, operation_id)`` is unique in ``SyncReceipt``.  Terminal results are stored and replayed; an id reused with a
  different payload is refused (``operation_id_reused``).  Transient failures store nothing, so they can be retried.
* Authority: authorization, assignment eligibility, version pinning, answer validation, rule evaluation, revision checks and the
  submission itself are all done by the Phase 9 services at sync time.  Client timestamps are diagnostic only.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from apps.assignments.constants import AssignmentStatus
from apps.assignments.selectors import assignments_visible_to
from apps.core.services import log_activity
from apps.core.utils import parse_uuid

from .. import selectors, services
from ..constants import ExecutionStatus
from ..models import ChecklistResponse, SyncReceipt
from ..services.responses import AnswerInput
from . import compat
from .package import canonical, sha256

RESPONSE_OPS = ("upsert_response", "clear_response")
OPS = (*RESPONSE_OPS, "submit")
ALLOWED_KEYS = {"operation_id", "type", "seq", "assignment_id", "execution_id", "version_id", "item_id", "value", "not_applicable", "comment",
                "base_revision", "client_created_at", "schema_version"}


class Outcome:
    """The result of one operation, with whether it is terminal (stored as a receipt)."""

    def __init__(self, status: str, *, code: str = "", message: str = "", terminal: bool = True, **extra: Any) -> None:
        self.status, self.code, self.message, self.terminal, self.extra = status, code, message, terminal, extra

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if self.code:
            out["code"] = self.code
        if self.message:
            out["message"] = self.message
        return {**out, **self.extra}


def fingerprint(op: dict[str, Any]) -> str:
    """What makes two operations 'the same': everything except the sequence number, timestamps and client execution hint."""
    keys = ("type", "assignment_id", "version_id", "item_id", "value", "not_applicable", "comment", "base_revision")
    return sha256({k: op.get(k) for k in keys})


def validate_envelope(op: Any) -> tuple[dict[str, Any] | None, str]:
    """Shape-check one operation.  Returns ``(clean, "")`` or ``(None, reason)``.  Nothing arbitrary is accepted."""
    if not isinstance(op, dict):
        return None, "The operation must be an object."
    unknown = sorted(set(op) - ALLOWED_KEYS)
    if unknown:
        return None, f"Unknown fields: {', '.join(unknown)}."
    clean = dict(op)
    if parse_uuid(clean.get("operation_id")) is None:
        return None, "operation_id must be a UUID."
    if clean.get("type") not in OPS:
        return None, "Unsupported operation type."
    if clean.get("schema_version", compat.SCHEMA_VERSION) != compat.SCHEMA_VERSION:
        return None, "Unsupported schema version; update the application."
    for key in ("assignment_id", "version_id"):
        if parse_uuid(clean.get(key)) is None:
            return None, f"{key} must be a UUID."
    if clean.get("execution_id") not in (None, "") and parse_uuid(clean["execution_id"]) is None:
        return None, "execution_id must be a UUID."
    if not isinstance(clean.get("seq", 0), int) or isinstance(clean.get("seq"), bool):
        return None, "seq must be an integer."
    if clean["type"] in RESPONSE_OPS:
        if parse_uuid(clean.get("item_id")) is None:
            return None, "item_id must be a UUID."
        base = clean.get("base_revision", 0)
        if not isinstance(base, int) or isinstance(base, bool) or base < 0:
            return None, "base_revision must be a non-negative integer."
        if len(canonical(clean.get("value"))) > compat.MAX_VALUE_BYTES:
            return None, "The answer is too large."
        if not isinstance(clean.get("comment", ""), (str, type(None))):
            return None, "comment must be text."
        if not isinstance(clean.get("not_applicable", False), bool):
            return None, "not_applicable must be true or false."
    return clean, ""


def _server_state(execution: Any, item_id: Any) -> dict[str, Any]:
    row = ChecklistResponse.objects.filter(execution=execution, item_id=item_id, is_hidden=False).first()
    if row is None:
        return {"exists": False, "revision": 0}
    return {"exists": True, "status": row.status, "value": row.value, "comment": row.comment, "revision": row.revision}


def _resolve_assignment(user: Any, op: dict[str, Any]):  # noqa: ANN202
    """``(assignment, None)`` or ``(None, Outcome)``.  Unknown / foreign / out-of-scope all answer ``unavailable``."""
    assignment = selectors.assignment_for_work(user, op["assignment_id"])
    if assignment is not None:
        return assignment, None
    seen = assignments_visible_to(user).filter(pk=parse_uuid(op["assignment_id"])).first()
    if seen is not None and seen.status == AssignmentStatus.CANCELLED:
        return None, Outcome("conflict", code="assignment_cancelled", message="This assignment was cancelled. Your local answers are kept on this device.")
    if seen is not None and seen.status == AssignmentStatus.COMPLETED:
        return None, Outcome("conflict", code="execution_submitted", message="This checklist was already completed and cannot be changed offline.")
    return None, Outcome("rejected", code="unavailable", terminal=False,
                         message="This assignment is no longer available to you. Your local answers are kept on this device.")


def _apply(user: Any, op: dict[str, Any]) -> Outcome:
    assignment, failure = _resolve_assignment(user, op)
    if failure:
        return failure
    if str(assignment.version_id) != str(parse_uuid(op["version_id"])):
        return Outcome("rejected", code="stale_version", message="The checklist version on this device does not match the assignment. Prepare it again.")
    try:
        execution, _created = services.start_or_resume(assignment, actor=user)
    except PermissionDenied:
        return Outcome("rejected", code="forbidden", message="You no longer have permission to complete this checklist.")
    except ValidationError as exc:
        return Outcome("rejected", code=getattr(exc, "execution_code", "not_executable"), message=" ".join(exc.messages))
    if op.get("execution_id") and str(parse_uuid(op["execution_id"])) != str(execution.pk):
        return Outcome("rejected", code="execution_mismatch", message="This operation belongs to a different execution.")
    if execution.status == ExecutionStatus.SUBMITTED and op["type"] != "submit":
        extra = {"server": _server_state(execution, op.get("item_id"))} if op["type"] in RESPONSE_OPS else {}
        return Outcome("conflict", code="execution_submitted", message="This checklist was already submitted and cannot be changed offline. Your local answer is kept.", **extra)
    if execution.status == ExecutionStatus.CANCELLED:
        return Outcome("conflict", code="execution_cancelled", message="This execution was cancelled. Your local answers are kept on this device.")
    if op["type"] == "submit":
        try:
            result = services.submit(execution, actor=user)
        except PermissionDenied:
            return Outcome("rejected", code="forbidden", message="You no longer have permission to submit this checklist.")
        except services.ExecutionValidationError as exc:
            return Outcome("rejected", code="validation_failed", message=str(exc.message), problems=exc.problems)
        except ValidationError as exc:
            return Outcome("rejected", code=getattr(exc, "execution_code", "not_submittable"), message=" ".join(exc.messages))
        return Outcome("applied", execution=str(execution.pk), execution_status=result.execution.status, already_submitted=result.already_submitted)
    clearing = op["type"] == "clear_response"
    inp = AnswerInput(str(op["item_id"]), None if clearing else op.get("value"), False if clearing else bool(op.get("not_applicable")),
                      "" if clearing else op.get("comment"), int(op.get("base_revision", 0)))
    try:
        saved = services.save_answers(execution, actor=user, inputs=[inp])
    except PermissionDenied:
        return Outcome("rejected", code="forbidden", message="You no longer have permission to complete this checklist.")
    except ValidationError as exc:
        code = getattr(exc, "execution_code", "")
        if code == "already_submitted":
            return Outcome("conflict", code="execution_submitted", message="This checklist was already submitted and cannot be changed offline.")
        return Outcome("conflict", code="execution_cancelled" if code == "not_editable" else code or "not_editable", message=" ".join(exc.messages))
    if saved.errors:
        return Outcome("rejected", code="validation_failed", message="The answer is not valid.", errors=next(iter(saved.errors.values())))
    if saved.conflicts:
        return Outcome("conflict", code="revision_mismatch", message="This answer was changed elsewhere. Compare the versions and choose.",
                       server=_server_state(execution, op["item_id"]))
    state = _server_state(execution, op["item_id"])
    return Outcome("applied", execution=str(execution.pk), response_revision=state["revision"], execution_revision=saved.execution.revision)


def _store(user: Any, op: dict[str, Any], outcome: Outcome, fp: str) -> SyncReceipt:
    from apps.assignments.models import ChecklistAssignment

    assignment = ChecklistAssignment.objects.filter(pk=parse_uuid(op["assignment_id"])).select_related("organization").first()
    organization = assignment.organization if assignment else user.organization_memberships.first().organization
    return SyncReceipt.objects.create(
        organization=organization, user=user, operation_id=parse_uuid(op["operation_id"]), op_type=op["type"],
        assignment=assignment, execution_id=outcome.extra.get("execution"), fingerprint=fp, status=outcome.status, result=outcome.body())


def process_operation(user: Any, op: dict[str, Any]) -> dict[str, Any]:
    """One operation -> its result dict (never raises for business outcomes)."""
    op_id, fp = parse_uuid(op["operation_id"]), fingerprint(op)
    prior = SyncReceipt.objects.filter(user=user, operation_id=op_id).first()
    if prior is not None:
        if prior.fingerprint != fp:
            return {"operation_id": str(op_id), "status": "rejected", "code": "operation_id_reused",
                    "message": "This operation id was already used for a different change."}
        return {"operation_id": str(op_id), **prior.result, "duplicate": True}
    try:
        with transaction.atomic():
            outcome = _apply(user, op)
            if outcome.terminal and outcome.status in ("applied", "conflict", "rejected"):
                _store(user, op, outcome, fp)  # in the same transaction as the change: both or neither
    except IntegrityError:  # a concurrent request with the same operation id won the race: replay its receipt
        winner = SyncReceipt.objects.filter(user=user, operation_id=op_id).first()
        if winner is not None and winner.fingerprint == fp:
            return {"operation_id": str(op_id), **winner.result, "duplicate": True}
        raise
    return {"operation_id": str(op_id), **outcome.body()}


def process_batch(user: Any, operations: list[Any]) -> dict[str, Any]:
    """Process ``operations`` in ``seq`` order; see the module docstring for the contract."""
    results: dict[int, dict[str, Any]] = {}
    valid: list[tuple[int, dict[str, Any]]] = []
    for index, raw in enumerate(operations):
        clean, reason = validate_envelope(raw)
        if clean is None:
            results[index] = {"operation_id": str(raw.get("operation_id")) if isinstance(raw, dict) else None, "status": "rejected",
                              "code": "invalid_operation", "message": reason}
        else:
            valid.append((index, clean))
    valid.sort(key=lambda pair: (pair[1].get("seq", 0), pair[0]))
    failed_assignments: set[str] = set()
    touched: dict[str, int] = {}
    for index, op in valid:
        assignment_key = str(parse_uuid(op["assignment_id"]))
        if op["type"] == "submit" and assignment_key in failed_assignments:
            results[index] = {"operation_id": str(parse_uuid(op["operation_id"])), "status": "blocked", "code": "dependency_failed",
                              "message": "An earlier answer for this checklist was not accepted, so it was not submitted."}
            continue
        result = process_operation(user, op)
        results[index] = result
        if result["status"] in ("conflict", "rejected"):
            failed_assignments.add(assignment_key)
        if result["status"] == "applied" and not result.get("duplicate"):
            touched[assignment_key] = touched.get(assignment_key, 0) + 1
    _audit(user, touched, results)
    ordered = [results[i] for i in range(len(operations))]
    counts = {s: sum(1 for r in ordered if r["status"] == s) for s in ("applied", "conflict", "rejected", "blocked")}
    return {"schema_version": compat.SCHEMA_VERSION, "results": ordered, "counts": counts}


def _audit(user: Any, touched: dict[str, int], results: dict[int, dict[str, Any]]) -> None:
    from apps.assignments.models import ChecklistAssignment

    for key, applied in touched.items():
        assignment = ChecklistAssignment.objects.filter(pk=key).select_related("organization").first()
        if assignment is not None:
            log_activity(verb="execution.offline_sync", organization=assignment.organization, actor=user, target=assignment,
                         description=f"Synchronized {applied} offline change(s) for {assignment.reference}",
                         metadata={"assignment_id": key, "applied": applied})


def resolve_conflict(user: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve a conflict the server reported: ``keep_server`` (discard the local value) or ``use_local`` (re-send it).

    The conflict must be one *this user* received (a stored ``conflict`` receipt).  ``use_local`` goes through the same
    validation, revision check and services as any other write, so a stale or invalid choice is refused again.  Both outcomes are
    audited as ``execution.conflict_resolved``.
    """
    conflict_id, new_id = parse_uuid(payload.get("conflict_operation_id")), parse_uuid(payload.get("operation_id"))
    resolution = payload.get("resolution")
    if conflict_id is None or new_id is None or resolution not in ("keep_server", "use_local"):
        return {"status": "rejected", "code": "invalid_operation", "message": "conflict_operation_id, operation_id and a valid resolution are required."}
    conflict = SyncReceipt.objects.filter(user=user, operation_id=conflict_id, status="conflict").select_related("assignment__organization").first()
    if conflict is None or conflict.op_type not in RESPONSE_OPS:
        return {"status": "rejected", "code": "unknown_conflict", "message": "There is no such conflict to resolve."}
    if conflict.result.get("code") != "revision_mismatch":
        return {"status": "rejected", "code": "not_resolvable", "message": "This conflict cannot be resolved here; the local answer stays on the device."}
    server = conflict.result.get("server") or {}
    base = {"operation_id": str(new_id), "assignment_id": str(conflict.assignment_id), "version_id": payload.get("version_id"),
            "item_id": payload.get("item_id"), "seq": 0}
    if resolution == "keep_server":
        op = {**base, "type": "upsert_response", "value": server.get("value"), "not_applicable": server.get("status") == "not_applicable",
              "comment": server.get("comment", ""), "base_revision": int(server.get("revision", 0))}
        prior = SyncReceipt.objects.filter(user=user, operation_id=new_id).first()
        if prior is not None:
            return {"operation_id": str(new_id), **prior.result, "duplicate": True, "resolution": resolution}
        outcome = Outcome("applied", resolution="keep_server", execution=str(conflict.execution_id) if conflict.execution_id else None)
        try:
            with transaction.atomic():
                _store(user, {**op, "type": "upsert_response"}, outcome, sha256({"resolve": "keep_server", "conflict": str(conflict_id)}))
        except IntegrityError:
            prior = SyncReceipt.objects.filter(user=user, operation_id=new_id).first()
            return {"operation_id": str(new_id), **(prior.result if prior else {}), "duplicate": True, "resolution": resolution}
        result = {"operation_id": str(new_id), **outcome.body(), "server": server}
    else:
        op = {**base, "type": "upsert_response", "value": payload.get("value"), "not_applicable": bool(payload.get("not_applicable")),
              "comment": payload.get("comment"), "base_revision": payload.get("base_revision", 0)}
        clean, reason = validate_envelope(op)
        if clean is None:
            return {"status": "rejected", "code": "invalid_operation", "message": reason}
        result = {**process_operation(user, clean), "resolution": resolution}
    organization = conflict.assignment.organization if conflict.assignment_id else conflict.organization
    if result.get("status") == "applied" and not result.get("duplicate"):
        log_activity(verb="execution.conflict_resolved", organization=organization, actor=user, target=conflict.assignment or conflict,
                     description=f"Resolved a synchronization conflict ({resolution.replace('_', ' ')})",
                     metadata={"assignment_id": str(conflict.assignment_id), "conflict_operation_id": str(conflict_id), "resolution": resolution,
                               "item_id": payload.get("item_id")})
    return result


__all__ = ["OPS", "Outcome", "fingerprint", "process_batch", "process_operation", "resolve_conflict", "validate_envelope"]

