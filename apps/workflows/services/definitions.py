"""Workflow definition management: definitions, draft versions, states, transitions, validation, publication, cloning, archiving.

Every write checks the actor's design permission (organization-wide; platform superuser for system workflows), validates its input against the
controlled vocabularies (target registry, handler registry, assignee policies, separation options, Phase 6 conditions) and is audited.  Published
versions are immutable: these services refuse to touch them (409) and the database triggers refuse everything else.  Lock order: definition -> version.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.services import log_activity, stamp_history

from .. import assignment, conditions, handlers, policies, validation
from ..constants import (
    ActorRule, ApprovalMode, Decision, DefinitionStatus, InstanceStatus, OnNoAssignee, StateKind, VersionStatus,
)
from ..models import WorkflowDefinition, WorkflowInstance, WorkflowState, WorkflowTransition, WorkflowVersion
from ..targets import TargetNotFound, get_spec
from .base import VERBS, integrity_error
from .errors import WorkflowConflict, WorkflowError, WorkflowValidationError

_CODE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
UNSET = object()


def _audit(verb: str, definition: WorkflowDefinition, actor: Any, description: str, **metadata: Any) -> None:
    log_activity(verb=verb, organization=definition.organization, actor=actor, target=definition, description=description,
                 metadata={"workflow_definition_id": str(definition.pk), "workflow": definition.code, **metadata})


def _text(value: Any, label: str, maximum: int, *, required: bool = False) -> str:
    text = (value or "").strip() if isinstance(value, str) or value is None else None
    if text is None or (required and not text) or len(text) > maximum:
        raise WorkflowError(_("%(label)s is required and at most %(n)d characters.") % {"label": label, "n": maximum}, "invalid_text")
    return text


def _code(value: Any, label: str = "The code") -> str:
    value = (value or "").strip().lower() if isinstance(value, str) else ""
    if not _CODE.match(value):
        raise WorkflowError(_("%(label)s may use lowercase letters, digits, - and _ (up to 40 characters).") % {"label": label}, "invalid_code")
    return value


def _lock_definition(pk: Any) -> WorkflowDefinition:
    return WorkflowDefinition.objects.select_for_update(of=("self",)).select_related("organization").get(pk=pk)


def _draft(version: WorkflowVersion) -> WorkflowVersion:
    """The version re-read under a lock, which must still be a draft (a published version is immutable)."""
    fresh = WorkflowVersion.objects.select_for_update(of=("self",)).select_related("definition__organization").get(pk=version.pk)
    if fresh.status != VersionStatus.DRAFT:
        raise WorkflowConflict(_("A %(status)s workflow version is immutable; create a new draft version instead.") % {"status": fresh.get_status_display().lower()},
                               "version_immutable")
    return fresh


# ---------------------------------------------------------------------------------------------------------------------------------
@transaction.atomic
def create_definition(*, actor: Any, organization: Any, code: str, name: str, target_type: str, description: str = "", is_default: bool = False,
                      auto_start_event: str = "") -> WorkflowDefinition:
    """A new definition with an empty draft (version 1).  ``organization=None`` creates a system workflow (platform superusers only)."""
    allowed = policies.can_create_system(actor) if organization is None else policies.can_create(actor, organization)
    if not allowed:
        raise PermissionDenied
    try:
        spec = get_spec(target_type)
    except TargetNotFound:
        raise WorkflowError(_("Choose a registered target type."), "invalid_target_type") from None
    if auto_start_event and auto_start_event not in spec.events:
        raise WorkflowError(_("That event does not exist for %(type)s records.") % {"type": spec.label.lower()}, "invalid_event")
    definition = WorkflowDefinition(organization=organization, code=_code(code), name=_text(name, _("The name"), 120, required=True),
                                    description=_text(description, _("The description"), 500), target_type=spec.type, auto_start_event=auto_start_event, created_by=actor)
    stamp_history(definition, actor, VERBS["definition_created"])
    try:
        with transaction.atomic():
            definition.save()
            if is_default:
                _make_default(definition)
            version = WorkflowVersion(definition=definition, number=1, created_by=actor, change_summary="Initial draft")
            stamp_history(version, actor)
            version.save()
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    _audit(VERBS["definition_created"], definition, actor, f"Created workflow {definition.name}", target_type=spec.type, system=organization is None)
    return definition


def _make_default(definition: WorkflowDefinition) -> None:
    WorkflowDefinition.objects.filter(organization=definition.organization, target_type=definition.target_type, is_default=True,
                                      status=DefinitionStatus.ACTIVE).exclude(pk=definition.pk).update(is_default=False)
    definition.is_default = True
    definition.save(update_fields=["is_default", "updated_at"])


@transaction.atomic
def update_definition(definition: WorkflowDefinition, *, actor: Any, name: Any = UNSET, description: Any = UNSET, is_default: Any = UNSET,
                      auto_start_event: Any = UNSET) -> WorkflowDefinition:
    """Name, description, the default flag and the auto-start event.  Code, organization and target type never change."""
    locked = _lock_definition(definition.pk)
    if not policies._design(actor, locked, "workflow.update_draft"):
        raise PermissionDenied
    if locked.status != DefinitionStatus.ACTIVE:
        raise WorkflowConflict(_("An archived workflow cannot be edited."), "archived")
    if name is not UNSET:
        locked.name = _text(name, _("The name"), 120, required=True)
    if description is not UNSET:
        locked.description = _text(description, _("The description"), 500)
    if auto_start_event is not UNSET:
        if auto_start_event and auto_start_event not in get_spec(locked.target_type).events:
            raise WorkflowError(_("That event does not exist for this target type."), "invalid_event")
        locked.auto_start_event = auto_start_event or ""
    stamp_history(locked, actor, VERBS["definition_updated"])
    try:
        with transaction.atomic():
            locked.save()
            if is_default is not UNSET:
                if is_default:
                    _make_default(locked)
                else:
                    locked.is_default = False
                    locked.save(update_fields=["is_default", "updated_at"])
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    _audit(VERBS["definition_updated"], locked, actor, f"Updated workflow {locked.name}")
    return locked


@transaction.atomic
def archive_definition(definition: WorkflowDefinition, *, actor: Any) -> WorkflowDefinition:
    """Archive: no new workflow starts; refused while instances are still running (they keep their exact version)."""
    locked = _lock_definition(definition.pk)
    if not policies.can_archive(actor, locked):
        raise PermissionDenied
    if locked.status == DefinitionStatus.ARCHIVED:
        raise WorkflowConflict(_("This workflow is already archived."), "already_archived")
    if WorkflowInstance.objects.filter(definition=locked, status=InstanceStatus.ACTIVE).exists():
        raise WorkflowConflict(_("Workflows of this definition are still running; finish or cancel them first."), "has_active_instances")
    locked.status, locked.is_default = DefinitionStatus.ARCHIVED, False
    stamp_history(locked, actor, VERBS["definition_archived"])
    locked.save()
    _audit(VERBS["definition_archived"], locked, actor, f"Archived workflow {locked.name}")
    return locked


# ---------------------------------------------------------------------------------------------------------------------------------
@transaction.atomic
def create_draft(definition: WorkflowDefinition, *, actor: Any, clone_from: WorkflowVersion | None = None, change_summary: str = "") -> WorkflowVersion:
    """A new draft version, cloned from ``clone_from`` (default: the published version) - independent of every running instance."""
    locked = _lock_definition(definition.pk)
    if not policies.can_create_version(actor, locked):
        raise PermissionDenied
    if locked.status != DefinitionStatus.ACTIVE:
        raise WorkflowConflict(_("An archived workflow cannot get new versions."), "archived")
    if locked.versions.filter(status=VersionStatus.DRAFT).exists():
        raise WorkflowConflict(_("There is already a draft version; edit or publish it first."), "draft_exists")
    source = clone_from or locked.versions.filter(status=VersionStatus.PUBLISHED).first()
    if source is not None and source.definition_id != locked.pk:
        raise PermissionDenied
    number = (locked.versions.aggregate(m=Max("number"))["m"] or 0) + 1
    version = WorkflowVersion(definition=locked, number=number, created_by=actor, change_summary=_text(change_summary, _("The summary"), 300) or
                              (f"Copy of version {source.number}" if source else "New draft"),
                              start_condition=source.start_condition if source else None, allow_loops=source.allow_loops if source else False)
    stamp_history(version, actor)
    try:
        with transaction.atomic():
            version.save()
            if source is not None:
                _copy_config(source, version)
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    _audit(VERBS["version_created"], locked, actor, f"Created draft version {number} of {locked.name}", version=number, cloned_from=source.number if source else None)
    return version


def _copy_config(source: WorkflowVersion, target: WorkflowVersion) -> None:
    mapping: dict[Any, WorkflowState] = {}
    for s in source.states.all().order_by("sort_order", "code"):
        mapping[s.pk] = WorkflowState.objects.create(
            version=target, code=s.code, name=s.name, description=s.description, kind=s.kind, sort_order=s.sort_order, assignee_policy=s.assignee_policy,
            approval_mode=s.approval_mode, min_approvals=s.min_approvals, separation=s.separation, due_hours=s.due_hours, on_no_assignee=s.on_no_assignee)
    for t in source.transitions.all().order_by("sort_order", "action"):
        WorkflowTransition.objects.create(version=target, action=t.action, label=t.label, source=mapping[t.source_id], target=mapping[t.target_id], handler=t.handler,
                                          actor_rule=t.actor_rule, decision=t.decision, reason_required=t.reason_required, condition=t.condition,
                                          sort_order=t.sort_order, enabled=t.enabled)


@transaction.atomic
def update_version(version: WorkflowVersion, *, actor: Any, change_summary: Any = UNSET, start_condition: Any = UNSET, allow_loops: Any = UNSET) -> WorkflowVersion:
    fresh = _draft(version)
    if not policies.can_edit_draft(actor, fresh):
        raise PermissionDenied
    if change_summary is not UNSET:
        fresh.change_summary = _text(change_summary, _("The summary"), 300)
    if start_condition is not UNSET:
        normalized, problems = conditions.clean_condition(fresh.definition.target_type, start_condition, "start condition")
        if problems:
            raise WorkflowError(" ".join(problems), "invalid_condition")
        fresh.start_condition = normalized
    if allow_loops is not UNSET:
        fresh.allow_loops = bool(allow_loops)
    stamp_history(fresh, actor, VERBS["version_updated"])
    fresh.save()
    _audit(VERBS["version_updated"], fresh.definition, actor, f"Updated draft version {fresh.number}", version=fresh.number)
    return fresh


# ---------------------------------------------------------------------------------------------------------------------------------
def _state_values(version: WorkflowVersion, data: dict[str, Any], current: WorkflowState | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "code" in data and current is None:
        out["code"] = _code(data["code"], "The state code")
    elif "code" in data and (data["code"] or "") != current.code:
        raise WorkflowError(_("The code of a state never changes (transitions refer to it)."), "code_immutable")
    if "name" in data:
        out["name"] = _text(data["name"], _("The name"), 120, required=True)
    if "description" in data:
        out["description"] = _text(data["description"], _("The description"), 300)
    if "kind" in data:
        if data["kind"] not in StateKind.values:
            raise WorkflowError(_("Unknown state kind."), "invalid_kind")
        out["kind"] = data["kind"]
    if "sort_order" in data:
        if isinstance(data["sort_order"], bool) or not isinstance(data["sort_order"], int) or not 0 <= data["sort_order"] <= 1000:
            raise WorkflowError(_("The order must be a whole number from 0 to 1000."), "invalid_order")
        out["sort_order"] = data["sort_order"]
    if "assignee_policy" in data:
        policy, problems = assignment.clean_policy(data["assignee_policy"], version.definition.organization)
        if problems:
            raise WorkflowError(" ".join(problems), "invalid_assignee_policy")
        out["assignee_policy"] = policy
    if "separation" in data:
        options, problems = assignment.clean_separation(data["separation"])
        if problems:
            raise WorkflowError(" ".join(problems), "invalid_separation")
        out["separation"] = options
    if "approval_mode" in data:
        if data["approval_mode"] not in ApprovalMode.values:
            raise WorkflowError(_("Unknown approval mode."), "invalid_approval_mode")
        out["approval_mode"] = data["approval_mode"]
    if "min_approvals" in data:
        if isinstance(data["min_approvals"], bool) or not isinstance(data["min_approvals"], int) or not 1 <= data["min_approvals"] <= 20:
            raise WorkflowError(_("The minimum number of approvals must be from 1 to 20."), "invalid_approval_count")
        out["min_approvals"] = data["min_approvals"]
    if "due_hours" in data:
        due = data["due_hours"]
        if due is not None and (isinstance(due, bool) or not isinstance(due, int) or not 1 <= due <= 24 * 365):
            raise WorkflowError(_("The deadline must be from 1 hour to a year, or empty."), "invalid_due")
        out["due_hours"] = due
    if "on_no_assignee" in data:
        if data["on_no_assignee"] not in OnNoAssignee.values:
            raise WorkflowError(_("Unknown no-assignee policy."), "invalid_policy")
        out["on_no_assignee"] = data["on_no_assignee"]
    return out


@transaction.atomic
def add_state(version: WorkflowVersion, *, actor: Any, **data: Any) -> WorkflowState:
    fresh = _draft(version)
    if not policies.can_edit_draft(actor, fresh):
        raise PermissionDenied
    values = _state_values(fresh, data, None)
    if "code" not in values or "name" not in values:
        raise WorkflowError(_("A state needs a code and a name."), "required_field")
    if "sort_order" not in values:
        values["sort_order"] = (fresh.states.aggregate(m=Max("sort_order"))["m"] or 0) + 10
    try:
        with transaction.atomic():
            state = WorkflowState.objects.create(version=fresh, **values)
    except IntegrityError as exc:
        name = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "")
        if name == "one_initial_workflow_state":
            raise WorkflowConflict(_("The version already has an initial state."), "initial_exists") from exc
        if name == "unique_workflow_state_code":
            raise WorkflowConflict(_("A state with this code already exists."), "duplicate_code") from exc
        raise integrity_error(exc) from exc
    _audit(VERBS["config_changed"], fresh.definition, actor, f"Added state {state.name}", version=fresh.number, state=state.code)
    return state


@transaction.atomic
def update_state(state: WorkflowState, *, actor: Any, **data: Any) -> WorkflowState:
    fresh = _draft(state.version)
    if not policies.can_edit_draft(actor, fresh):
        raise PermissionDenied
    current = WorkflowState.objects.select_for_update().get(pk=state.pk, version=fresh)
    for key, value in _state_values(fresh, data, current).items():
        setattr(current, key, value)
    try:
        with transaction.atomic():
            current.save()
    except IntegrityError as exc:
        name = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "")
        raise (WorkflowConflict(_("The version already has an initial state."), "initial_exists") if name == "one_initial_workflow_state" else integrity_error(exc)) from exc
    _audit(VERBS["config_changed"], fresh.definition, actor, f"Changed state {current.name}", version=fresh.number, state=current.code)
    return current


@transaction.atomic
def delete_state(state: WorkflowState, *, actor: Any) -> None:
    fresh = _draft(state.version)
    if not policies.can_edit_draft(actor, fresh):
        raise PermissionDenied
    current = WorkflowState.objects.select_for_update().get(pk=state.pk, version=fresh)
    if WorkflowTransition.objects.filter(version=fresh).filter(source=current).exists() or WorkflowTransition.objects.filter(version=fresh, target=current).exists():
        raise WorkflowConflict(_("Remove the transitions that use this state first."), "state_in_use")
    code = current.code
    current.delete()
    _audit(VERBS["config_changed"], fresh.definition, actor, f"Removed state {code}", version=fresh.number, state=code)


# ---------------------------------------------------------------------------------------------------------------------------------
def _transition_values(version: WorkflowVersion, data: dict[str, Any], current: WorkflowTransition | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for end in ("source", "target"):
        if end in data:
            state = data[end] if isinstance(data[end], WorkflowState) else WorkflowState.objects.filter(version=version, code=data[end]).first()
            if state is None or state.version_id != version.pk:
                raise WorkflowError(_("Choose a state of this version for the %(end)s.") % {"end": end}, f"invalid_{end}")
            out[end] = state
    if "action" in data:
        out["action"] = _code(data["action"], "The action code")
    if "label" in data:
        out["label"] = _text(data["label"], _("The label"), 80, required=True)
    if "handler" in data:
        handler = (data["handler"] or "").strip()
        if handler and handler not in {h.code for h in handlers.for_target(version.definition.target_type)}:
            raise WorkflowError(_("That business action is not registered for %(type)s records.") % {"type": version.definition.target_type}, "invalid_handler")
        out["handler"] = handler
    if "actor_rule" in data:
        if data["actor_rule"] not in ActorRule.values:
            raise WorkflowError(_("Unknown actor rule."), "invalid_actor_rule")
        out["actor_rule"] = data["actor_rule"]
    if "decision" in data:
        if data["decision"] not in Decision.values:
            raise WorkflowError(_("Unknown decision kind."), "invalid_decision")
        out["decision"] = data["decision"]
    if "reason_required" in data:
        out["reason_required"] = bool(data["reason_required"])
    if "enabled" in data:
        out["enabled"] = bool(data["enabled"])
    if "sort_order" in data:
        if isinstance(data["sort_order"], bool) or not isinstance(data["sort_order"], int) or not 0 <= data["sort_order"] <= 1000:
            raise WorkflowError(_("The order must be a whole number from 0 to 1000."), "invalid_order")
        out["sort_order"] = data["sort_order"]
    if "condition" in data:
        normalized, problems = conditions.clean_condition(version.definition.target_type, data["condition"], "condition")
        if problems:
            raise WorkflowError(" ".join(problems), "invalid_condition")
        out["condition"] = normalized
    return out


@transaction.atomic
def add_transition(version: WorkflowVersion, *, actor: Any, **data: Any) -> WorkflowTransition:
    fresh = _draft(version)
    if not policies.can_edit_draft(actor, fresh):
        raise PermissionDenied
    values = _transition_values(fresh, data, None)
    for needed in ("source", "target", "action", "label"):
        if needed not in values:
            raise WorkflowError(_("A transition needs a source, a target, an action code and a label."), "required_field")
    if "sort_order" not in values:
        values["sort_order"] = (WorkflowTransition.objects.filter(version=fresh, source=values["source"], action=values["action"]).aggregate(m=Max("sort_order"))["m"] or 0) + 10
    try:
        with transaction.atomic():
            transition = WorkflowTransition.objects.create(version=fresh, **values)
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    _audit(VERBS["config_changed"], fresh.definition, actor, f"Added transition {transition.action}", version=fresh.number, source=transition.source.code, target=transition.target.code)
    return transition


@transaction.atomic
def update_transition(transition: WorkflowTransition, *, actor: Any, **data: Any) -> WorkflowTransition:
    fresh = _draft(transition.version)
    if not policies.can_edit_draft(actor, fresh):
        raise PermissionDenied
    current = WorkflowTransition.objects.select_for_update().select_related("source", "target").get(pk=transition.pk, version=fresh)
    for key, value in _transition_values(fresh, data, current).items():
        setattr(current, key, value)
    try:
        with transaction.atomic():
            current.save()
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    _audit(VERBS["config_changed"], fresh.definition, actor, f"Changed transition {current.action}", version=fresh.number)
    return current


@transaction.atomic
def delete_transition(transition: WorkflowTransition, *, actor: Any) -> None:
    fresh = _draft(transition.version)
    if not policies.can_edit_draft(actor, fresh):
        raise PermissionDenied
    current = WorkflowTransition.objects.get(pk=transition.pk, version=fresh)
    action = current.action
    current.delete()
    _audit(VERBS["config_changed"], fresh.definition, actor, f"Removed transition {action}", version=fresh.number)


# ---------------------------------------------------------------------------------------------------------------------------------
def validate_version(version: WorkflowVersion, *, actor: Any) -> validation.Report:
    """Read-only validation (also run, again, by publication)."""
    if not (policies.can_validate(actor, version) or policies.can_view_definition(actor, version.definition)):
        raise PermissionDenied
    report = validation.validate(version)
    _audit(VERBS["version_validated"], version.definition, actor, f"Validated version {version.number}", version=version.number, errors=len(report.errors), warnings=len(report.warnings))
    return report


def config_hash(version: WorkflowVersion) -> str:
    """SHA-256 of the canonical configuration: stored at publication as proof of what was reviewed and what runs."""
    states = [[s.code, s.name, s.kind, s.sort_order, s.assignee_policy, s.approval_mode, s.min_approvals, s.separation, s.due_hours, s.on_no_assignee]
              for s in version.states.order_by("code")]
    transitions = [[t.source.code, t.action, t.sort_order, t.target.code, t.label, t.handler, t.actor_rule, t.decision, t.reason_required, t.condition, t.enabled]
                   for t in version.transitions.select_related("source", "target").order_by("source__code", "action", "sort_order")]
    blob = json.dumps({"target": version.definition.target_type, "start": version.start_condition, "loops": version.allow_loops, "states": states, "transitions": transitions},
                      sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


@transaction.atomic
def publish(version: WorkflowVersion, *, actor: Any) -> WorkflowVersion:
    """Validate and publish a draft: the previous published version becomes superseded (running instances keep theirs); the version is sealed."""
    definition = _lock_definition(version.definition_id)
    fresh = _draft(version)
    if not policies.can_publish(actor, fresh):
        raise PermissionDenied
    if definition.status != DefinitionStatus.ACTIVE:
        raise WorkflowConflict(_("An archived workflow cannot be published."), "archived")
    report = validation.validate(fresh)
    if not report.ok:
        raise WorkflowValidationError(report)
    WorkflowVersion.objects.filter(definition=definition, status=VersionStatus.PUBLISHED).update(status=VersionStatus.SUPERSEDED)
    fresh.status, fresh.published_by, fresh.published_at, fresh.config_hash = VersionStatus.PUBLISHED, actor, timezone.now(), config_hash(fresh)
    stamp_history(fresh, actor, VERBS["version_published"])
    try:
        with transaction.atomic():
            fresh.save()
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    _audit(VERBS["version_published"], definition, actor, f"Published version {fresh.number} of {definition.name}", version=fresh.number, config_hash=fresh.config_hash,
           warnings=len(report.warnings))
    return fresh


@transaction.atomic
def archive_version(version: WorkflowVersion, *, actor: Any) -> WorkflowVersion:
    """Archive a *superseded* version (the published one is replaced by publishing, or archived with its definition)."""
    definition = _lock_definition(version.definition_id)
    if not policies.can_archive(actor, definition):
        raise PermissionDenied
    fresh = WorkflowVersion.objects.select_for_update(of=("self",)).get(pk=version.pk)
    if fresh.status == VersionStatus.ARCHIVED:
        raise WorkflowConflict(_("This version is already archived."), "already_archived")
    if fresh.status != VersionStatus.SUPERSEDED:
        raise WorkflowConflict(_("Only a superseded version can be archived."), "invalid_status")
    if WorkflowInstance.objects.filter(version=fresh, status=InstanceStatus.ACTIVE).exists():
        raise WorkflowConflict(_("Workflows are still running on this version."), "has_active_instances")
    fresh.status = VersionStatus.ARCHIVED
    stamp_history(fresh, actor, VERBS["version_archived"])
    fresh.save()
    _audit(VERBS["version_archived"], definition, actor, f"Archived version {fresh.number}", version=fresh.number)
    return fresh
