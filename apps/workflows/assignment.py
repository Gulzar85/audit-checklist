"""Assignee resolution and separation of duties (ADR 0072).

An assignee policy names *who may be asked*, from a controlled vocabulary (specific users, holders of a registered permission, holders of a role);
it is resolved against the live RBAC data at the moment a state is entered and re-checked when a task is claimed or acted on.  The engine never
picks an arbitrary user: a ``user`` policy creates one task per named person, a ``permission`` / ``role`` policy creates **one claimable task** for the
group.  No reporting-line hierarchy is assumed.

Separation of duties is a list of controlled options on the state; whatever a designer chooses, the platform's own rules (the domain services')
still apply on top - a workflow can only add restrictions, never remove them.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model

from apps.permissions import registry
from apps.permissions.authorization import can_access_location, has_permission, has_role

from .constants import SEPARATION_OPTIONS, AssigneeType

MAX_CANDIDATES = 500


def clean_policy(policy: Any, organization: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate an assignee policy.  ``None`` / empty means "no task" (a waiting state)."""
    if policy in (None, {}, ""):
        return None, []
    if not isinstance(policy, dict):
        return None, ["The assignee policy must be an object."]
    kind = policy.get("type")
    if kind not in AssigneeType.values:
        return None, ["The assignee policy type must be one of: " + ", ".join(AssigneeType.values) + "."]
    if kind == AssigneeType.USER:
        ids = policy.get("users")
        if organization is None:
            return None, ["A system workflow cannot name specific users; use a permission or a role."]
        if not isinstance(ids, list) or not ids or len(ids) > 20 or len(set(map(str, ids))) != len(ids):
            return None, ["Name 1 to 20 distinct users."]
        found = get_user_model().objects.filter(pk__in=ids, organization_memberships__organization=organization, organization_memberships__is_active=True).distinct()
        if found.count() != len(ids):
            return None, ["Every named user must be a member of the organization."]
        return {"type": "user", "users": [str(i) for i in ids]}, []
    if kind == AssigneeType.PERMISSION:
        code = policy.get("permission")
        if not isinstance(code, str) or not registry.is_registered(code):
            return None, ["Choose a registered permission."]
        return {"type": "permission", "permission": code}, []
    slug = policy.get("role")
    if not isinstance(slug, str) or not slug.strip() or len(slug) > 80:
        return None, ["Name a role."]
    return {"type": "role", "role": slug.strip()}, []


def clean_separation(options: Any) -> tuple[list[str], list[str]]:
    if options in (None, ""):
        return [], []
    if not isinstance(options, list) or len(set(options)) != len(options):
        return [], ["Separation options must be a list without duplicates."]
    unknown = [o for o in options if o not in SEPARATION_OPTIONS]
    if unknown:
        return [], ["Unknown separation option(s): " + ", ".join(map(str, unknown)) + "."]
    return list(options), []


def violations(actor: Any, instance: Any, spec: Any, obj: Any, options: list[str]) -> list[str]:
    """The separation options the actor would break by acting (empty = allowed)."""
    broken: list[str] = []
    for option in options or []:
        if option == "not_starter" and instance.started_by_id is not None and instance.started_by_id == actor.pk:
            broken.append(option)
        elif option == "not_target_owner" and spec.owner_id(obj) is not None and spec.owner_id(obj) == actor.pk:
            broken.append(option)
        elif option == "not_target_creator" and spec.creator_id(obj) is not None and spec.creator_id(obj) == actor.pk:
            broken.append(option)
        elif option == "not_previous_actor":
            from .models import WorkflowEvent

            if WorkflowEvent.objects.filter(instance=instance, actor=actor, kind__in=["transition", "approval"]).exists():
                broken.append(option)
    return broken


def eligible(user: Any, policy: dict[str, Any], organization: Any, location: Any) -> bool:
    """Is ``user`` an eligible assignee under ``policy`` right now (active, an active member, and matching the policy)?"""
    if not (getattr(user, "is_active", False) and getattr(user, "is_authenticated", True)):
        return False
    from apps.organizations.models import OrganizationMembership

    if not OrganizationMembership.objects.filter(user=user, organization=organization, is_active=True).exists():
        return False
    kind = policy.get("type")
    if kind == AssigneeType.USER:
        return str(user.pk) in {str(i) for i in policy.get("users", [])} and can_access_location(user, location)
    if kind == AssigneeType.PERMISSION:
        return has_permission(user, policy["permission"], organization=organization, location=location)
    if kind == AssigneeType.ROLE:
        return has_role(user, policy["role"], organization=organization) and can_access_location(user, location)
    return False


def candidates(policy: dict[str, Any], organization: Any, location: Any) -> list[Any]:
    """The users who currently satisfy the policy (bounded; ordered by email so results are deterministic)."""
    model = get_user_model()
    if policy.get("type") == AssigneeType.USER:
        pool = model.objects.filter(pk__in=policy.get("users", []))
    else:
        pool = model.objects.filter(is_active=True, organization_memberships__organization=organization, organization_memberships__is_active=True).distinct()
    return [u for u in pool.order_by("email")[:MAX_CANDIDATES] if eligible(u, policy, organization, location)]
