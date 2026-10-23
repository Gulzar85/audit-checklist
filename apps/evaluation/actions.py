"""The controlled **action registry**: the only things a matched rule may cause.

A rule never touches a business model.  Each action type is a small handler that calls the *owning module's service* and reports a structured outcome:

* ``request_finding`` -> ``apps.findings.services.create_from_evaluation`` (idempotent on the action key; the Findings engine owns numbering, classification and audit);
* ``start_workflow`` -> ``apps.workflows.services.start`` for the registered ``execution`` target (a workflow that already runs for the record is *skipped*, never duplicated);
* ``tag`` -> recorded on the run only (a stable label for later modules: notifications, reporting).

Handlers run only for a stored *final* run, after the evaluation was recorded, each in its own savepoint: an action failure is recorded as that action's
failure and never rolls back the evaluation, the submission, or a sibling action.  Corrective actions are deliberately not an action type (the contract
for them would delegate to ``apps.corrective_actions.services``; nothing is built until a requirement exists).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import constants as k


@dataclass
class ActionOutcome:
    status: str
    result_type: str = ""
    result_id: str = ""
    message: str = ""


def _request_finding(execution: Any, ctx: dict[str, Any], params: dict[str, Any]) -> ActionOutcome:
    from apps.findings.services import FindingError, create_from_evaluation

    try:
        finding, created = create_from_evaluation(
            execution, rule_code=ctx["rule_code"], rule_name=ctx["rule_name"], detection_key=ctx["action_key"], category_code=params["category"],
            severity_code=params["severity"], title=params["title"], description=params.get("description", ""), requires_review=bool(params.get("requires_review")))
    except FindingError as exc:
        return ActionOutcome(k.ActionStatus.FAILED, message=" ".join(getattr(exc, "messages", [str(exc)]))[:500])
    return ActionOutcome(k.ActionStatus.SUCCEEDED if created else k.ActionStatus.SKIPPED, "finding", str(finding.pk),
                         f"Created {finding.reference}" if created else f"{finding.reference} already exists")


def _start_workflow(execution: Any, ctx: dict[str, Any], params: dict[str, Any]) -> ActionOutcome:
    from apps.workflows.services import WorkflowConflict, WorkflowError, start

    try:
        instance = start(actor=None, target_type="execution", target=execution, definition_code=params["workflow"], system=True)
    except WorkflowConflict as exc:
        if getattr(exc, "workflow_code", "") == "already_running":
            return ActionOutcome(k.ActionStatus.SKIPPED, message="A workflow of this kind is already running for the execution")
        return ActionOutcome(k.ActionStatus.FAILED, message=" ".join(exc.messages)[:500])
    except WorkflowError as exc:
        return ActionOutcome(k.ActionStatus.FAILED, message=f"{getattr(exc, 'workflow_code', 'workflow_error')}: {' '.join(exc.messages)}"[:500])
    return ActionOutcome(k.ActionStatus.SUCCEEDED, "workflow", str(instance.pk), f"Started {instance.reference}")


def _tag(execution: Any, ctx: dict[str, Any], params: dict[str, Any]) -> ActionOutcome:
    return ActionOutcome(k.ActionStatus.SUCCEEDED, "tag", params["tag"], f"Tagged “{params['tag']}”")


HANDLERS = {k.ActionType.REQUEST_FINDING: _request_finding, k.ActionType.START_WORKFLOW: _start_workflow, k.ActionType.TAG: _tag}


def run_action(execution: Any, action: dict[str, Any], ctx: dict[str, Any]) -> ActionOutcome:
    """Run one registered action; an unregistered type is a failure (never executed)."""
    handler = HANDLERS.get(action.get("type"))
    if handler is None:
        return ActionOutcome(k.ActionStatus.FAILED, message="Unsupported action type")
    return handler(execution, ctx, action)
