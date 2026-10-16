"""Write operations for roles, permissions and role assignments.

Services own writes (selectors own reads, policies own decisions).  Every change is atomic,
audited in ``ActivityLog`` and stamped for django-simple-history.  Services trust their caller
to have *authorized* the action (``apps.permissions.policies``) but still enforce the data
rules that must hold for every writer: system roles are immutable, a role and a location must
belong to the member's organization, and registered permissions only.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.core.services import apply_changes, log_activity, stamp_history

from . import registry
from .models import MembershipRole, Permission, Role, RolePermission

VERBS = {
    "role_created": "role.created",
    "role_updated": "role.updated",
    "role_deactivated": "role.deactivated",
    "role_activated": "role.activated",
    "permissions_changed": "role.permissions_changed",
    "role_assigned": "membership_role.assigned",
    "role_removed": "membership_role.removed",
    "scope_changed": "membership_role.scope_changed",
    "membership_status": "membership.status_changed",
}


# ---------------------------------------------------------------------------
# Registry mirror
# ---------------------------------------------------------------------------
@transaction.atomic
def sync_registry() -> dict[str, int]:
    """Make the ``Permission`` table match the registry (idempotent).

    Creates missing rows, refreshes labels, and *deactivates* rows whose code left the
    registry (never deletes: roles may still reference them in history).
    """
    created = updated = deactivated = 0
    existing = {permission.code: permission for permission in Permission.objects.all()}
    for definition in registry.REGISTRY:
        row = existing.pop(definition.code, None)
        wanted = {
            "resource": definition.resource,
            "action": definition.action,
            "label": definition.label,
            "description": definition.description,
            "is_active": True,
        }
        if row is None:
            Permission.objects.create(code=definition.code, **wanted)
            created += 1
        elif any(getattr(row, name) != value for name, value in wanted.items()):
            for name, value in wanted.items():
                setattr(row, name, value)
            row.save()
            updated += 1
    for stale in existing.values():
        if stale.is_active:
            stale.is_active = False
            stale.save(update_fields=["is_active", "updated_at"])
            deactivated += 1
    return {"created": created, "updated": updated, "deactivated": deactivated}


def _permissions_for(codes: Iterable[str]) -> list[Permission]:
    wanted = list(dict.fromkeys(codes))
    unknown = [code for code in wanted if not registry.is_registered(code)]
    if unknown:
        raise ValidationError({"permissions": _("Unknown permission: %(codes)s") % {"codes": ", ".join(unknown)}})
    rows = {p.code: p for p in Permission.objects.filter(code__in=wanted, is_active=True)}
    missing = [code for code in wanted if code not in rows]
    if missing:  # registered in code but not mirrored yet
        sync_registry()
        rows = {p.code: p for p in Permission.objects.filter(code__in=wanted, is_active=True)}
    return [rows[code] for code in wanted if code in rows]


@transaction.atomic
def seed_system_roles() -> dict[str, int]:
    """Create or refresh the platform roles from the registry (idempotent).

    A system role's *permissions* are reset to the seeded set (they are platform-defined);
    nothing else about a customer's data is touched.
    """
    sync_registry()
    created = refreshed = 0
    for slug, (name, description, codes) in registry.SYSTEM_ROLES.items():
        role, was_created = Role.objects.get_or_create(
            organization=None, slug=slug,
            defaults={"name": name, "description": description, "is_system": True},
        )
        if was_created:
            created += 1
        else:
            refreshed += 1
            changed = [n for n, v in (("name", name), ("description", description), ("is_system", True), ("is_active", True))
                       if getattr(role, n) != v]
            if changed:
                role.name, role.description, role.is_system, role.is_active = name, description, True, True
                role.save()
        _write_permissions(role, codes)
    return {"created": created, "refreshed": refreshed}


def _write_permissions(role: Role, codes: Iterable[str]) -> tuple[set[str], set[str]]:
    wanted = {p.code: p for p in _permissions_for(codes)}
    current = {rp.permission.code: rp for rp in role.role_permissions.select_related("permission")}
    added = set(wanted) - set(current)
    removed = set(current) - set(wanted)
    for code in added:
        RolePermission.objects.create(role=role, permission=wanted[code])
    for code in removed:
        current[code].delete()
    return added, removed


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
def _unique_slug(organization: Any, name: str) -> str:
    base = slugify(name)[:70] or "role"
    slug, n = base, 2
    while Role.objects.filter(organization=organization, slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _guard_editable(role: Role) -> None:
    if role.is_system:
        raise ValidationError(_("System roles are managed by the platform and cannot be changed."))


@transaction.atomic
def create_role(
    *, organization: Any, name: str, description: str = "", permissions: Iterable[str] = (), actor: Any = None
) -> Role:
    """Create a custom role owned by ``organization``."""
    if organization is None:
        raise ValidationError(_("A custom role belongs to an organization."))
    name = (name or "").strip()
    if not name:
        raise ValidationError({"name": _("A role needs a name.")})
    if Role.objects.filter(organization=organization, name__iexact=name).exists():
        raise ValidationError({"name": _("This organization already has a role called “%(name)s”.") % {"name": name}})
    role = Role(organization=organization, name=name, slug=_unique_slug(organization, name),
                description=description.strip(), is_system=False)
    stamp_history(role, actor, VERBS["role_created"])
    role.save()
    added, _removed = _write_permissions(role, permissions)
    log_activity(
        verb=VERBS["role_created"], organization=organization, actor=actor, target=role,
        description=f"Created role {role.name}", metadata={"permissions": sorted(added)},
    )
    return role


@transaction.atomic
def update_role(role: Role, *, actor: Any = None, **data: Any) -> Role:
    """Rename / re-describe a custom role (``name``, ``description``)."""
    _guard_editable(role)
    allowed = {k: v for k, v in data.items() if k in ("name", "description")}
    if "name" in allowed:
        allowed["name"] = (allowed["name"] or "").strip()
        if not allowed["name"]:
            raise ValidationError({"name": _("A role needs a name.")})
        if Role.objects.filter(organization=role.organization, name__iexact=allowed["name"]).exclude(pk=role.pk).exists():
            raise ValidationError({"name": _("This organization already has a role with that name.")})
    changes = apply_changes(role, allowed)
    if changes:
        stamp_history(role, actor, VERBS["role_updated"])
        role.save()
        log_activity(verb=VERBS["role_updated"], organization=role.organization, actor=actor, target=role,
                     description=f"Updated role {role.name}", metadata={"changes": changes})
    return role


@transaction.atomic
def set_role_permissions(role: Role, codes: Iterable[str], *, actor: Any = None) -> Role:
    """Replace a custom role's permission set (audited as added/removed codes)."""
    _guard_editable(role)
    added, removed = _write_permissions(role, codes)
    if added or removed:
        stamp_history(role, actor, VERBS["permissions_changed"])
        role.save(update_fields=["updated_at"])
        log_activity(
            verb=VERBS["permissions_changed"], organization=role.organization, actor=actor, target=role,
            description=f"Changed permissions of {role.name}",
            metadata={"added": sorted(added), "removed": sorted(removed)},
        )
    return role


@transaction.atomic
def deactivate_role(role: Role, *, actor: Any = None) -> Role:
    """Switch a custom role off: its assignments stop granting anything, nothing is deleted."""
    _guard_editable(role)
    if role.is_active:
        role.is_active = False
        stamp_history(role, actor, VERBS["role_deactivated"])
        role.save()
        log_activity(verb=VERBS["role_deactivated"], organization=role.organization, actor=actor, target=role,
                     description=f"Deactivated role {role.name}")
    return role


@transaction.atomic
def activate_role(role: Role, *, actor: Any = None) -> Role:
    _guard_editable(role)
    if not role.is_active:
        role.is_active = True
        stamp_history(role, actor, VERBS["role_activated"])
        role.save()
        log_activity(verb=VERBS["role_activated"], organization=role.organization, actor=actor, target=role,
                     description=f"Reactivated role {role.name}")
    return role


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------
def _validate_assignment(membership: Any, role: Role, scope_type: str, location: Any) -> None:
    if not membership.is_active:
        raise ValidationError(_("Roles can only be assigned to an active membership."))
    if not role.is_active:
        raise ValidationError({"role": _("This role is inactive.")})
    if role.organization_id not in (None, membership.organization_id):
        raise ValidationError({"role": _("This role belongs to a different organization.")})
    if scope_type not in MembershipRole.ScopeType.values:
        raise ValidationError({"scope_type": _("Unknown scope.")})
    if scope_type == MembershipRole.ScopeType.ORGANIZATION:
        if location is not None:
            raise ValidationError({"location": _("An organization-wide assignment has no location.")})
    else:
        if location is None:
            raise ValidationError({"location": _("Choose the location this role applies to.")})
        if location.organization_id != membership.organization_id:
            raise ValidationError({"location": _("The location must belong to the member's organization.")})
        if not location.is_active:
            raise ValidationError({"location": _("This location is inactive.")})


@transaction.atomic
def assign_role(
    membership: Any, role: Role, *, scope_type: str = MembershipRole.ScopeType.ORGANIZATION,
    location: Any = None, actor: Any = None,
) -> MembershipRole:
    """Grant ``role`` to ``membership`` within a scope (idempotent; re-activates a removed one)."""
    _validate_assignment(membership, role, scope_type, location)
    lookup = MembershipRole.objects.filter(membership=membership, role=role)
    lookup = lookup.filter(location__isnull=True) if location is None else lookup.filter(location=location, scope_type=scope_type)
    assignment = lookup.first()
    if assignment is not None and assignment.is_active:
        return assignment
    if assignment is None:
        assignment = MembershipRole(membership=membership, role=role, scope_type=scope_type, location=location)
    assignment.is_active = True
    assignment.assigned_by = actor if getattr(actor, "is_authenticated", False) else None
    stamp_history(assignment, actor, VERBS["role_assigned"])
    assignment.save()
    log_activity(
        verb=VERBS["role_assigned"], organization=membership.organization, actor=actor, target=assignment,
        description=f"Assigned {role.name} to {membership.user}",
        metadata={"user_id": str(membership.user_id), "role": role.slug, "scope_type": scope_type,
                  "location_id": str(location.pk) if location else None},
    )
    return assignment


@transaction.atomic
def remove_role(assignment: MembershipRole, *, actor: Any = None) -> MembershipRole:
    """Withdraw an assignment (kept inactive for history, so it can be audited and restored)."""
    if assignment.is_active:
        assignment.is_active = False
        stamp_history(assignment, actor, VERBS["role_removed"])
        assignment.save()
        membership = assignment.membership
        log_activity(
            verb=VERBS["role_removed"], organization=membership.organization, actor=actor, target=assignment,
            description=f"Removed {assignment.role.name} from {membership.user}",
            metadata={"user_id": str(membership.user_id), "role": assignment.role.slug,
                      "scope_type": assignment.scope_type},
        )
    return assignment


@transaction.atomic
def assign_scope(
    assignment: MembershipRole, *, scope_type: str, location: Any = None, actor: Any = None
) -> MembershipRole:
    """Change where an existing assignment applies."""
    _validate_assignment(assignment.membership, assignment.role, scope_type, location)
    previous = {"scope_type": assignment.scope_type,
                "location_id": str(assignment.location_id) if assignment.location_id else None}
    if previous["scope_type"] == scope_type and assignment.location_id == (location.pk if location else None):
        return assignment
    duplicate = MembershipRole.objects.filter(
        membership=assignment.membership, role=assignment.role, location=location,
    ).exclude(pk=assignment.pk)
    if scope_type != MembershipRole.ScopeType.ORGANIZATION:
        duplicate = duplicate.filter(scope_type=scope_type)
    if duplicate.exists():
        raise ValidationError(_("The member already holds this role at that scope."))
    assignment.scope_type = scope_type
    assignment.location = location
    stamp_history(assignment, actor, VERBS["scope_changed"])
    assignment.save()
    log_activity(
        verb=VERBS["scope_changed"], organization=assignment.membership.organization, actor=actor, target=assignment,
        description=f"Changed the scope of {assignment.role.name} for {assignment.membership.user}",
        metadata={"from": previous, "to": {"scope_type": scope_type,
                                           "location_id": str(location.pk) if location else None}},
    )
    return assignment


@transaction.atomic
def change_membership_status(membership: Any, status: str, *, actor: Any = None) -> Any:
    """Move a membership between pending/active/suspended/inactive (delegates to the identity service).

    A membership that is not *active* grants nothing, whatever roles it holds; the roles stay
    on the row so reinstating the member restores their access exactly.
    """
    from apps.accounts import services as account_services

    return account_services.set_membership_status(membership, status, actor=actor)
