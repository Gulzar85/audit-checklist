"""The attachment **target registry** (ADR 0060): the only way evidence can be linked to a business record.

A client never names a Django model.  It names a registered ``target_type`` plus opaque references; trusted server code resolves the
references, checks existence, organization, the caller's authorization and the target's state, and answers what evidence the target
accepts.  There is no ``GenericForeignKey`` and no client-controlled ``content_type`` / ``object_id``.

Adding a target type (findings, corrective actions, verifications ...) is deliberate:
1. add a value to ``constants.TargetType`` and the explicit nullable foreign key(s) on ``EvidenceAttachment`` (+ migration, + check
   constraint, + trigger update);
2. subclass ``TargetSpec`` here, resolving through that module's *selector* (so an invisible record is "not found") and authorizing through
   that module's *policy* (so evidence can never widen access);
3. ``register()`` it.  Nothing else in the evidence code changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.exceptions import PermissionDenied

from apps.checklists.answer_types import registry as answer_registry
from apps.core.utils import parse_uuid

from .constants import Kind, TargetType


class TargetNotFound(Exception):
    """Unknown type, malformed/unknown/invisible reference or a record from another organization - all the same generic answer."""


class TargetLocked(Exception):
    """The target exists but is in a state that does not allow this operation (e.g. a submitted execution)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class TargetContext:
    target_type: str
    organization: Any
    execution: Any = None
    item: Any = None
    label: str = ""
    kind_hint: str = ""
    allowed_extensions: tuple[str, ...] | None = None
    max_files: int | None = None
    accepted_kinds: tuple[str, ...] = ()
    #: Corrective-action targets (Phase 13): the action, and the stage the file belongs to (plan / progress / completion / verification / rejection).
    corrective_action: Any = None
    purpose: str = ""

    @property
    def key(self) -> dict[str, Any]:
        """The explicit foreign keys that identify this target on ``EvidenceAttachment`` (also the filter for its live attachments)."""
        if self.corrective_action is not None:
            return {"corrective_action": self.corrective_action}
        return {"execution": self.execution, "item": self.item}

    @property
    def target_obj(self) -> Any:
        return self.corrective_action if self.corrective_action is not None else self.execution

    @property
    def reference(self) -> str:
        return getattr(self.target_obj, "reference", "")


class TargetSpec:
    type: str = ""

    def resolve(self, user: Any, ref: dict[str, Any], *, create: bool = False) -> TargetContext:  # pragma: no cover - interface
        raise NotImplementedError

    def context_for(self, attachment: Any) -> TargetContext:  # pragma: no cover - interface
        raise NotImplementedError

    def can_view(self, user: Any, ctx: TargetContext) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def check_can_attach(self, user: Any, ctx: TargetContext) -> None:  # pragma: no cover - interface
        """Raise ``PermissionDenied`` (not allowed) or ``TargetLocked`` (wrong state)."""
        raise NotImplementedError

    def lock(self, ctx: TargetContext) -> Any:  # pragma: no cover - interface
        """Lock the target row ``FOR UPDATE`` (serialises attach / detach / state changes), store it on ``ctx`` and return it."""
        raise NotImplementedError

    def touch(self, ctx: TargetContext, *, activity: bool = False) -> None:  # pragma: no cover - interface
        """Record that the locked target changed (a revision bump for optimistic-concurrency tokens)."""
        raise NotImplementedError

    def audit_meta(self, ctx: TargetContext) -> dict[str, Any]:  # pragma: no cover - interface
        """Identifiers added to the audit entries of an upload / attach / detach (never file contents)."""
        raise NotImplementedError


class ChecklistResponseTarget(TargetSpec):
    """One checklist item within one execution (the Phase 9 response)."""

    type = TargetType.CHECKLIST_RESPONSE

    def resolve(self, user: Any, ref: dict[str, Any], *, create: bool = False) -> TargetContext:
        from apps.executions import selectors, services
        from apps.executions.services.evaluation import build_state

        item_id = parse_uuid(ref.get("item_id"))
        if item_id is None:
            raise TargetNotFound
        execution = None
        if ref.get("execution_id"):
            execution_id = parse_uuid(ref["execution_id"])
            execution = selectors.get_execution(user, execution_id) if execution_id else None
        elif ref.get("assignment_id"):
            assignment = selectors.assignment_for_work(user, ref["assignment_id"])
            if assignment is not None:
                execution = selectors.execution_for_assignment(user, assignment)
                if execution is None and create:
                    try:
                        execution, _created = services.start_or_resume(assignment, actor=user)
                    except PermissionDenied:
                        raise
                    except Exception as exc:  # a business refusal (e.g. assignment not open): not a target
                        raise TargetNotFound from exc
        if execution is None:
            raise TargetNotFound
        state = build_state(execution)
        spec = state.item_for(item_id)  # an item of *this execution's version* only
        if spec is None:
            raise TargetNotFound
        result = state.evaluation.items[spec.code]
        accepts = spec.answer_type in (Kind.PHOTO, Kind.FILE, Kind.SIGNATURE) or bool(result.evidence)
        if not accepts or not result.visible:
            raise TargetNotFound
        from apps.checklists.models import ChecklistItem

        item = ChecklistItem.objects.get(pk=item_id)
        definition_kinds = tuple((result.evidence or {}).get("kinds", ()))
        kind_hint = spec.answer_type if spec.answer_type in (Kind.PHOTO, Kind.FILE, Kind.SIGNATURE) else (definition_kinds[0] if len(definition_kinds) == 1 else "")
        cfg = spec.config if spec.answer_type in (Kind.PHOTO, Kind.FILE) else {}
        exts = tuple(cfg.get("allowed_extensions", ())) or None
        return TargetContext(
            target_type=self.type, organization=execution.organization, execution=execution, item=item, label=spec.question, kind_hint=str(kind_hint),
            allowed_extensions=exts, max_files=cfg.get("max_files"), accepted_kinds=definition_kinds or ((spec.answer_type,) if kind_hint else ()),
        )

    def context_for(self, attachment: Any) -> TargetContext:
        return TargetContext(target_type=self.type, organization=attachment.organization, execution=attachment.execution, item=attachment.item, label=attachment.item.question)

    def can_view(self, user: Any, ctx: TargetContext) -> bool:
        """Evidence is stricter than the execution itself: the person must *currently* hold an execution permission at the location.

        (Phase 9 lets whoever started an execution keep seeing it; for files that would outlive a revoked role or membership.)
        """
        from apps.core.policies import is_active_user
        from apps.executions import policies
        from apps.permissions.authorization import has_permission
        from apps.permissions.registry import P

        location = ctx.execution.assignment.location
        current = has_permission(user, P.EXECUTION_VIEW, location=location) or has_permission(user, P.EXECUTION_UPDATE, location=location)
        return bool(is_active_user(user) and current and policies.can_view(user, ctx.execution))

    def lock(self, ctx: TargetContext) -> Any:
        from apps.executions.services.base import lock_execution

        ctx.execution = lock_execution(ctx.execution.pk)
        return ctx.execution

    def touch(self, ctx: TargetContext, *, activity: bool = False) -> None:
        from django.utils import timezone

        locked = ctx.execution
        locked.revision += 1
        if activity:
            locked.last_activity_at = timezone.now()
            locked.save(update_fields=["revision", "last_activity_at", "updated_at"])
        else:
            locked.save(update_fields=["revision", "updated_at"])

    def audit_meta(self, ctx: TargetContext) -> dict[str, Any]:
        return {"execution_id": str(ctx.execution.pk), "assignment_id": str(ctx.execution.assignment_id), "item": ctx.item.code}

    def check_can_attach(self, user: Any, ctx: TargetContext) -> None:
        from apps.executions import policies
        from apps.executions.constants import ExecutionStatus

        if not policies.can_work_at(user, ctx.execution):
            raise PermissionDenied
        status = ctx.execution.status
        if status == ExecutionStatus.SUBMITTED:
            raise TargetLocked("already_submitted", "This checklist was already submitted; evidence can no longer be changed.")
        if status != ExecutionStatus.IN_PROGRESS:
            raise TargetLocked("not_editable", "This execution is no longer open for changes.")


_REGISTRY: dict[str, TargetSpec] = {}


def register(spec: TargetSpec) -> None:
    _REGISTRY[spec.type] = spec


def get_spec(target_type: Any) -> TargetSpec:
    spec = _REGISTRY.get(str(target_type))
    if spec is None:
        raise TargetNotFound
    return spec


def registered_types() -> list[str]:
    return sorted(_REGISTRY)


register(ChecklistResponseTarget())
_ = answer_registry  # (the answer-type registry decides which items take evidence; imported for the dependency to be explicit)
