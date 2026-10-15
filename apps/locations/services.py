"""Business operations for the location domain.

Every write to the tree - create, update, move, activate, deactivate - goes
through here, so hierarchy validation, locking, history and activity logging are
applied uniformly by the web UI, the API and imports.

Rules worth knowing (documented in ADR 0014):

* A location is **deactivated, never deleted**.
* **An active location cannot sit beneath an inactive parent.**  Consequently a
  location with active descendants can only be deactivated together with them
  (``cascade=True``), and a location can only be activated once its parent is.
  Inactive subtrees are therefore always whole, and "active locations" never
  needs an ancestor walk.
* Deactivating never touches checklists, executions or any other history; those
  later phases reference locations with ``PROTECT`` and keep working.
* Writes are refused while the organization is not ``active``.

Concurrency: every tree-changing operation first takes a row lock on the owning
organization (``SELECT ... FOR NO KEY UPDATE``).  Two administrators changing the
same organization's hierarchy are therefore serialised, and the validation each
one runs always sees the other's committed result.  PostgreSQL triggers enforce
the structural rules again at the lowest level as a safety net.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from simple_history.utils import bulk_update_with_history

from apps.core.services import apply_changes, log_activity, stamp_history
from apps.organizations.models import Organization

from .models import Location, LocationType
from .selectors import get_descendants

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
_DB_RULES = {
    "unique_location_code_per_organization": (
        "code",
        _("A location with this code already exists in this organization."),
    ),
    "unique_location_type_code_per_organization": (
        "code",
        _("A location type with this code already exists in this organization."),
    ),
    "unique_global_location_type_code": (
        "code",
        _("A platform location type with this code already exists."),
    ),
    "location_cannot_be_its_own_parent": ("parent", _("A location cannot be its own parent.")),
    "location_parent_same_organization": (
        "parent",
        _("The parent location must belong to the same organization."),
    ),
    "location_type_same_organization": (
        "location_type",
        _("The location type must belong to the same organization (or be platform-defined)."),
    ),
    "location_hierarchy_cycle": (
        "parent",
        _("A location cannot be moved beneath one of its own descendants."),
    ),
    "location_organization_immutable": (
        "organization",
        _("A location cannot be moved to another organization."),
    ),
    "location_type_organization_in_use": (
        "organization",
        _("Locations of other organizations already use this type."),
    ),
}


def as_validation_error(exc: IntegrityError) -> ValidationError:
    """Translate a database integrity failure into a user-facing validation error."""
    diagnostics = getattr(exc.__cause__, "diag", None)
    name = getattr(diagnostics, "constraint_name", "") or ""
    if name in _DB_RULES:
        field, message = _DB_RULES[name]
        return ValidationError({field: message}, code=name)
    return ValidationError(
        _("The change conflicts with the current location hierarchy. Reload and try again."),
        code="conflict",
    )


def lock_organization(organization_id: Any) -> Organization:
    """Lock and return the organization row; serialises tree writes per tenant."""
    return Organization.objects.select_for_update(no_key=True).get(pk=organization_id)


def require_active_organization(organization: Organization) -> None:
    if not organization.is_active:
        raise ValidationError(
            {
                "organization": _(
                    "Organization â€œ%(name)sâ€ is %(status)s. Reactivate it before changing its locations."
                )
                % {"name": organization.name, "status": organization.get_status_display().lower()}
            },
            code="organization_inactive",
        )


def _ensure_type_assignable(location_type_id: Any, previous_type_id: Any = None) -> None:
    """A *newly assigned* location type must be active (existing assignments may stay)."""
    if location_type_id and location_type_id != previous_type_id:
        if not LocationType.objects.filter(pk=location_type_id, is_active=True).exists():
            raise ValidationError(
                {"location_type": _("This location type is inactive and cannot be assigned.")}
            )


def _lock_location(location: Location) -> Location:
    """Lock the organization, then return a fresh, row-locked copy of ``location``."""
    lock_organization(location.organization_id)
    return Location.objects.select_for_update().select_related("organization").get(pk=location.pk)


# ---------------------------------------------------------------------------
# Location types
# ---------------------------------------------------------------------------
@transaction.atomic
def create_location_type(*, actor: Any = None, **data: Any) -> LocationType:
    """Create a location type for an organization (or a platform-defined one)."""
    organization = data.get("organization")
    if organization is not None:
        organization = lock_organization(organization.pk)
        require_active_organization(organization)
        data["organization"] = organization

    location_type = LocationType(**data)
    location_type.full_clean()
    stamp_history(location_type, actor, "location_type.created")
    try:
        location_type.save()
    except IntegrityError as exc:
        raise as_validation_error(exc) from exc
    log_activity(
        verb="location_type.created",
        organization=location_type.organization,
        actor=actor,
        description=f"Created location type {location_type.name} ({location_type.code})",
        target=location_type,
    )
    return location_type


@transaction.atomic
def update_location_type(
    location_type: LocationType, *, actor: Any = None, **data: Any
) -> LocationType:
    """Edit a location type.  Its organization (or platform scope) never changes."""
    if "is_active" in data:
        raise ValueError("Use activate_location_type()/deactivate_location_type().")

    if location_type.organization_id:
        require_active_organization(lock_organization(location_type.organization_id))
    current = LocationType.objects.select_for_update().get(pk=location_type.pk)

    if "organization" in data:
        requested = getattr(data.pop("organization"), "pk", None)
        if requested != current.organization_id:
            raise ValidationError(
                {"organization": _("A location type cannot be moved to another organization.")}
            )

    changes = apply_changes(current, data)
    if not changes:
        return current
    current.full_clean()
    stamp_history(current, actor, "location_type.updated")
    try:
        current.save()
    except IntegrityError as exc:
        raise as_validation_error(exc) from exc
    log_activity(
        verb="location_type.updated",
        organization=current.organization,
        actor=actor,
        description=f"Updated location type {current.name} ({current.code})",
        target=current,
        metadata={"changes": changes},
    )
    return current


@transaction.atomic
def _set_location_type_active(
    location_type: LocationType, is_active: bool, *, actor: Any = None
) -> LocationType:
    """Toggle a type.  Existing locations keep it; it just cannot be newly assigned."""
    if location_type.organization_id:
        require_active_organization(lock_organization(location_type.organization_id))
    current = LocationType.objects.select_for_update().get(pk=location_type.pk)
    if current.is_active == is_active:
        return current
    verb = "location_type.activated" if is_active else "location_type.deactivated"
    current.is_active = is_active
    stamp_history(current, actor, verb)
    current.save(update_fields=["is_active", "updated_at"])
    log_activity(
        verb=verb,
        organization=current.organization,
        actor=actor,
        description=f"Location type {current.name} {'activated' if is_active else 'deactivated'}",
        target=current,
    )
    return current


def activate_location_type(location_type: LocationType, *, actor: Any = None) -> LocationType:
    return _set_location_type_active(location_type, True, actor=actor)


def deactivate_location_type(location_type: LocationType, *, actor: Any = None) -> LocationType:
    return _set_location_type_active(location_type, False, actor=actor)


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
@transaction.atomic
def create_location(*, actor: Any = None, **data: Any) -> Location:
    """Create a location, validating its place in the hierarchy."""
    if data.get("organization") is None:
        raise ValidationError({"organization": _("An organization is required.")})
    data.pop("is_active", None)  # new locations always start active

    organization = lock_organization(data["organization"].pk)
    require_active_organization(organization)
    data["organization"] = organization

    location = Location(**data)
    location.full_clean()
    _ensure_type_assignable(location.location_type_id)
    stamp_history(location, actor, "location.created")
    try:
        location.save()
    except IntegrityError as exc:
        raise as_validation_error(exc) from exc
    log_activity(
        verb="location.created",
        organization=organization,
        actor=actor,
        description=f"Created location {location.name} ({location.code})",
        target=location,
        metadata={
            "code": location.code,
            "parent": str(location.parent_id) if location.parent_id else None,
            "location_type": str(location.location_type_id),
        },
    )
    return location


@transaction.atomic
def update_location(location: Location, *, actor: Any = None, **data: Any) -> Location:
    """Edit a location's details and/or move it by passing ``parent``.

    The organization of an existing location is immutable.  Lifecycle changes go
    through :func:`activate_location` / :func:`deactivate_location`.
    """
    if "is_active" in data:
        raise ValueError("Use activate_location()/deactivate_location() to change is_active.")

    current = _lock_location(location)
    require_active_organization(current.organization)

    if "organization" in data:
        requested = getattr(data.pop("organization"), "pk", None)
        if requested != current.organization_id:
            raise ValidationError(
                {"organization": _("A location cannot be moved to another organization.")}
            )

    previous_parent = current.parent
    previous_type_id = current.location_type_id
    changes = apply_changes(current, data)
    if not changes:
        return current

    current.full_clean()
    _ensure_type_assignable(current.location_type_id, previous_type_id)
    stamp_history(current, actor, "location.parent_changed" if "parent" in changes else "location.updated")
    try:
        current.save()
    except IntegrityError as exc:
        raise as_validation_error(exc) from exc

    if "parent" in changes:
        new_parent = current.parent
        log_activity(
            verb="location.parent_changed",
            organization=current.organization,
            actor=actor,
            description=f"Moved location {current.name} ({current.code})",
            target=current,
            metadata={
                "from_parent": changes["parent"]["from"],
                "to_parent": changes["parent"]["to"],
                "from_parent_name": previous_parent.name if previous_parent else None,
                "to_parent_name": new_parent.name if new_parent else None,
            },
        )
    other = {name: change for name, change in changes.items() if name != "parent"}
    if other:
        log_activity(
            verb="location.updated",
            organization=current.organization,
            actor=actor,
            description=f"Updated location {current.name} ({current.code})",
            target=current,
            metadata={"changes": other},
        )
    return current


def move_location(location: Location, new_parent: Location | None, *, actor: Any = None) -> Location:
    """Re-parent a location (``None`` makes it a root).

    Rejects self-parenting, cycles, cross-organization parents and inactive
    parents; serialised against concurrent moves in the same organization.
    """
    return update_location(location, actor=actor, parent=new_parent)


@transaction.atomic
def activate_location(location: Location, *, actor: Any = None) -> Location:
    """Reactivate a single location; its parent (if any) must already be active."""
    current = _lock_location(location)
    require_active_organization(current.organization)
    if current.is_active:
        return current
    if current.parent_id and not Location.objects.filter(pk=current.parent_id, is_active=True).exists():
        raise ValidationError(
            {"is_active": _("The parent location is inactive. Activate the parent first.")}
        )
    current.is_active = True
    stamp_history(current, actor, "location.activated")
    current.save(update_fields=["is_active", "updated_at"])
    log_activity(
        verb="location.activated",
        organization=current.organization,
        actor=actor,
        description=f"Location {current.name} ({current.code}) activated",
        target=current,
    )
    return current


@transaction.atomic
def deactivate_location(
    location: Location, *, actor: Any = None, cascade: bool = False
) -> Location:
    """Deactivate a location.

    If it has active descendants the call is refused unless ``cascade=True``, in
    which case the whole active subtree is deactivated in this one transaction
    (each node keeps its own history record; one activity entry records the
    business event).  Nothing is deleted.
    """
    current = _lock_location(location)
    require_active_organization(current.organization)
    if not current.is_active:
        return current

    active_descendants = list(get_descendants(current).filter(is_active=True))
    if active_descendants and not cascade:
        raise ValidationError(
            {
                "is_active": _(
                    "This location has %(count)d active descendant location(s). "
                    "Deactivate them first, or deactivate everything together."
                )
                % {"count": len(active_descendants)}
            },
            code="has_active_descendants",
        )

    now = timezone.now()
    targets = [current, *active_descendants]
    for target in targets:
        target.is_active = False
        target.updated_at = now
    bulk_update_with_history(
        targets,
        Location,
        ["is_active", "updated_at"],
        default_user=actor if getattr(actor, "is_authenticated", False) else None,
        default_change_reason="location.deactivated",
    )
    log_activity(
        verb="location.deactivated",
        organization=current.organization,
        actor=actor,
        description=f"Location {current.name} ({current.code}) deactivated",
        target=current,
        metadata={
            "cascade": bool(active_descendants),
            "descendant_count": len(active_descendants),
            "descendant_ids": [str(item.pk) for item in active_descendants[:100]],
        },
    )
    return current
