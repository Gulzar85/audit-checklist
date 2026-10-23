"""Registrations with the other phases, made once at startup (``apps.py``): nothing here is imported by those phases.

* Phase 11 evidence: the ``corrective_action`` attachment target (explicit key + a *purpose* per stage), authorized through this
  module's policies - so evidence can never widen access to an action.
* Phase 12 findings: the eligibility policy as resolve / cancel blockers and as the verification gate (ADR 0068).
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied

from apps.core.utils import parse_uuid
from apps.evidence.constants import TargetType
from apps.evidence.targets import TargetContext, TargetLocked, TargetNotFound, TargetSpec, register
from apps.findings import hooks as finding_hooks

from . import eligibility, policies, selectors
from .constants import PLANNING_STATUSES, Purpose


class CorrectiveActionTarget(TargetSpec):
    """Evidence attached to one corrective action, for one stage (``purpose``)."""

    type = TargetType.CORRECTIVE_ACTION

    def resolve(self, user: Any, ref: dict[str, Any], *, create: bool = False) -> TargetContext:
        action_id = parse_uuid(ref.get("corrective_action_id"))
        purpose = str(ref.get("purpose") or "")
        if action_id is None or purpose not in Purpose.values:
            raise TargetNotFound
        action = selectors.get_action(user, action_id)  # an invisible, foreign or unknown action is simply "not found"
        if action is None:
            raise TargetNotFound
        return TargetContext(target_type=self.type, organization=action.organization, corrective_action=action, label=action.title, purpose=purpose)

    def context_for(self, attachment: Any) -> TargetContext:
        return TargetContext(target_type=self.type, organization=attachment.organization, corrective_action=attachment.corrective_action,
                             label=attachment.corrective_action.title, purpose=attachment.purpose)

    def can_view(self, user: Any, ctx: TargetContext) -> bool:
        """Stricter than the action itself: a *current* view permission at the location (a former owner whose role was revoked loses the files)."""
        from apps.core.policies import is_active_user
        from apps.permissions.authorization import has_permission
        from apps.permissions.registry import P

        action = ctx.corrective_action
        current = has_permission(user, P.CORRECTIVE_ACTION_VIEW, location=action.location) or policies.is_owner(user, action) and has_permission(
            user, P.CORRECTIVE_ACTION_UPDATE_PROGRESS, location=action.location)
        return bool(is_active_user(user) and current and policies.can_view(user, action))

    def check_can_attach(self, user: Any, ctx: TargetContext) -> None:
        action = ctx.corrective_action
        if not policies.can_attach_evidence(user, action, ctx.purpose):
            from .constants import VERIFIER_PURPOSES, WORKER_PURPOSES

            allowed = WORKER_PURPOSES.get(ctx.purpose) or VERIFIER_PURPOSES.get(ctx.purpose) or ()
            if action.status not in allowed:
                code = "not_editable" if action.status in PLANNING_STATUSES or action.status == "submitted" else "already_" + action.status
                raise TargetLocked(code, f"Evidence of this kind cannot be added while the action is {action.get_status_display().lower()}.")
            raise PermissionDenied

    def lock(self, ctx: TargetContext) -> Any:
        from .models import CorrectiveAction

        locked = (CorrectiveAction.objects.select_for_update(of=("self",))
                  .select_related("organization", "location", "finding", "assigned_to").get(pk=ctx.corrective_action.pk))
        ctx.corrective_action = locked
        return locked

    def touch(self, ctx: TargetContext, *, activity: bool = False) -> None:
        ctx.corrective_action.save(update_fields=["updated_at"])

    def audit_meta(self, ctx: TargetContext) -> dict[str, Any]:
        return {"action_id": str(ctx.corrective_action.pk), "finding_id": str(ctx.corrective_action.finding_id)}


register(CorrectiveActionTarget())
finding_hooks.register_resolve_blocker(eligibility.resolution_blocker)
finding_hooks.register_cancel_blocker(eligibility.cancellation_blocker)
finding_hooks.register_verification_gate(eligibility.verification_gate)
