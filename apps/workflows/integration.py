"""Registrations made once at startup (``apps.py``): target types, business actions, domain guards and the auto-start receivers (ADR 0071).

* Phases 12 and 13 do not import this app: it registers a **governance guard** with them, so while a workflow governs a decision (the current state of an
  active instance has a transition whose business action is that decision) a *direct* call to the domain service is refused - the decision must go through
  the workflow, never around it.  A call made by the engine itself (``handlers.in_workflow()``) passes.
* Domain events (``corrective_action.submitted``, ``finding.raised`` ...) may auto-start a workflow when the organization (or the system) has a published,
  active definition with that ``auto_start_event``.  Receivers run after the domain transaction committed, so a workflow problem can never undo a domain change;
  a refusal (no assignee, start condition, already running) is audited, not raised.
"""

from __future__ import annotations

import logging
from typing import Any

from . import handlers, targets
from .constants import DefinitionStatus, InstanceStatus, VersionStatus

logger = logging.getLogger(__name__)

targets.register_defaults()
handlers.register_defaults()


def governed(target_type: str, obj: Any, domain_action: str) -> str | None:
    """A message when an active workflow governs this domain action on this record (and we are not inside the engine)."""
    if handlers.in_workflow():
        return None
    from .models import WorkflowInstance

    code = f"{target_type}.{domain_action}"
    for instance in WorkflowInstance.objects.filter(target_type=target_type, target_id=obj.pk, status=InstanceStatus.ACTIVE).select_related("state", "version", "definition"):
        if instance.version.transitions.filter(source=instance.state, enabled=True, handler=code).exists():
            return f"This decision is governed by the workflow “{instance.definition.name}” ({instance.reference}); complete it through the workflow task."
    return None


def _guard(target_type: str):  # noqa: ANN202
    def guard(obj: Any, name: str, actor: Any) -> str | None:
        return governed(target_type, obj, name)

    return guard


def _auto_start(event_code: str, target_type: str, obj: Any, actor: Any) -> None:
    from django.db.models import Q

    from apps.core.services import log_activity

    from .models import WorkflowDefinition
    from .services import WorkflowError, start

    spec = targets.get_spec(target_type)
    organization = spec.organization(obj)
    definitions = list(WorkflowDefinition.objects.filter(
        Q(organization=organization) | Q(organization__isnull=True), target_type=target_type, auto_start_event=event_code, status=DefinitionStatus.ACTIVE,
        versions__status=VersionStatus.PUBLISHED).distinct().order_by("organization"))  # organization-specific (non-null) definitions sort before system ones
    chosen = next((d for d in definitions if d.organization_id is not None), None) or next((d for d in definitions if d.organization_id is None), None)
    if chosen is None:
        return
    try:
        start(actor=actor, target_type=target_type, target=obj, definition=chosen, system=True)
    except Exception as exc:  # noqa: BLE001 - a workflow problem must never break the domain flow that raised the event
        code = getattr(exc, "workflow_code", exc.__class__.__name__)
        logger.info("Workflow auto-start skipped for %s %s: %s", target_type, obj.pk, code)
        log_activity(verb="workflow.auto_start_skipped", organization=organization, actor=actor if getattr(actor, "is_authenticated", False) else None, target=obj,
                     description=f"Workflow {chosen.code} was not started: {code}", metadata={"workflow": chosen.code, "target_type": target_type, "target_id": str(obj.pk), "code": str(code),
                                                                                            "detail": " ".join(getattr(exc, "messages", [])) if isinstance(exc, WorkflowError) else ""})


def _connect() -> None:
    from apps.corrective_actions import events as ca_events
    from apps.corrective_actions import hooks as ca_hooks
    from apps.findings import events as finding_events
    from apps.findings import hooks as finding_hooks

    ca_hooks.register_transition_guard(_guard("corrective_action"))
    finding_hooks.register_transition_guard(_guard("finding"))

    def on_submitted(sender, action, actor=None, **kwargs):  # noqa: ANN001, ANN003, ANN202
        _auto_start("corrective_action.submitted", "corrective_action", action, actor)

    def on_ca_created(sender, action, actor=None, **kwargs):  # noqa: ANN001, ANN003, ANN202
        _auto_start("corrective_action.created", "corrective_action", action, actor)

    def on_finding_raised(sender, finding, actor=None, **kwargs):  # noqa: ANN001, ANN003, ANN202
        _auto_start("finding.raised", "finding", finding, actor)

    ca_events.action_submitted.connect(on_submitted, dispatch_uid="workflows_auto_start_ca_submitted", weak=False)
    ca_events.action_created.connect(on_ca_created, dispatch_uid="workflows_auto_start_ca_created", weak=False)
    finding_events.finding_raised.connect(on_finding_raised, dispatch_uid="workflows_auto_start_finding_raised", weak=False)


_connect()
