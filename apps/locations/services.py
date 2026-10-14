"""Business operations for the location domain.

All writes (create, update, move, deactivate) go through here so that hierarchy
validation, transactions, row locking and activity logging are applied uniformly
by the web UI, the API, the admin and imports.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.services import log_activity

from .models import Location, LocationType
from .selectors import descendant_ids


# ---------------------------------------------------------------------------
# Location types
# ---------------------------------------------------------------------------
@transaction.atomic
def create_location_type(*, actor: Any = None, **data: Any) -> LocationType:
    location_type = LocationType(**data)
    location_type.full_clean()
    location_type.save()
    log_activity(
        verb="location_type.created",
        organization=location_type.organization,
        actor=actor,
        description=f"Created location type {location_type.name} ({location_type.code})",
        target=location_type,
    )
    return location_type


@transaction.atomic
def update_location_type(location_type: LocationType, *, actor: Any = None, **data: Any) -> LocationType:
    changed = _apply(location_type, data)
    if not changed:
        return location_type
    location_type.full_clean()
    location_type.save()
    log_activity(
        verb="location_type.updated",
        organization=location_type.organization,
        actor=actor,
        description=f"Updated location type {location_type.name}",
        target=location_type,
        metadata={"changed_fields": sorted(changed)},
    )
    return location_type


@transaction.atomic
def set_location_type_active(
    location_type: LocationType,
    is_active: bool,
    *,
    actor: Any = None,
) -> LocationType:
    if location_type.is_active == is_active:
        return location_type
    location_type.is_active = is_active
    location_type.save(update_fields=["is_active", "updated_at"])
    log_activity(
        verb="location_type.activated" if is_active else "location_type.deactivated",
        organization=location_type.organization,
        actor=actor,
        description=f"Location type {location_type.name} {'activated' if is_active else 'deactivated'}",
        target=location_type,
    )
    return location_type


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
@transaction.atomic
def create_location(*, actor: Any = None, **data: Any) -> Location:
    location = Location(**data)
    location.full_clean()
    location.save()
    log_activity(
        verb="location.created",
        organization=location.organization,
        actor=actor,
        description=f"Created location {location.name} ({location.code})",
        target=location,
    )
    return location


@transaction.atomic
def update_location(location: Location, *, actor: Any = None, **data: Any) -> Location:
    changed = _apply(location, data)
    if not changed:
        return location
    location.full_clean()
    location.save()
    log_activity(
        verb="location.updated",
        organization=location.organization,
        actor=actor,
        description=f"Updated location {location.name} ({location.code})",
        target=location,
        metadata={"changed_fields": sorted(changed)},
    )
    return location


@transaction.atomic
def move_location(location: Location, new_parent: Location | None, *, actor: Any = None) -> Location:
    """Re-parent a location, guarding against cross-org and circular moves.

    The row is locked for the duration of the transaction so two concurrent
    moves cannot race into an invalid hierarchy.
    """
    locked = Location.objects.select_for_update().get(pk=location.pk)
    previous_parent_id = locked.parent_id

    if new_parent is not None:
        if new_parent.pk == locked.pk:
            raise ValidationError({"parent": _("A location cannot be its own parent.")})
        if new_parent.organization_id != locked.organization_id:
            raise ValidationError({"parent": _("The parent must belong to the same organization.")})
        if new_parent.pk in descendant_ids(locked, include_self=True):
            raise ValidationError(
                {"parent": _("A location cannot be moved beneath one of its own descendants.")}
            )

    if previous_parent_id == (new_parent.pk if new_parent else None):
        return locked

    locked.parent = new_parent
    locked.full_clean()
    locked.save(update_fields=["parent", "updated_at"])
    log_activity(
        verb="location.parent_changed",
        organization=locked.organization,
        actor=actor,
        description=f"Moved location {locked.name} ({locked.code})",
        target=locked,
        metadata={
            "from_parent": str(previous_parent_id) if previous_parent_id else None,
            "to_parent": str(new_parent.pk) if new_parent else None,
        },
    )
    return locked


@transaction.atomic
def set_location_active(location: Location, is_active: bool, *, actor: Any = None) -> Location:
    if location.is_active == is_active:
        return location
    location.is_active = is_active
    location.save(update_fields=["is_active", "updated_at"])
    log_activity(
        verb="location.activated" if is_active else "location.deactivated",
        organization=location.organization,
        actor=actor,
        description=f"Location {location.name} ({location.code}) {'activated' if is_active else 'deactivated'}",
        target=location,
    )
    return location


def activate_location(location: Location, *, actor: Any = None) -> Location:
    return set_location_active(location, True, actor=actor)


def deactivate_location(location: Location, *, actor: Any = None) -> Location:
    return set_location_active(location, False, actor=actor)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _apply(instance: Any, data: dict[str, Any]) -> list[str]:
    """Assign changed fields onto ``instance`` and return the changed names."""
    changed: list[str] = []
    for field, value in data.items():
        if getattr(instance, field) != value:
            setattr(instance, field, value)
            changed.append(field)
    return changed
