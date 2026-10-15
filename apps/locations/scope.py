"""Location-scope *foundation* for organization memberships (Phase 2; ADR 0036).

Stores and reads ``User -> Organization -> allowed location (subtree)`` and nothing more.
No selector, policy or API consults it to restrict access - the scope engine is a later
phase - and a member with no scope rows is *not* restricted.  Keeping the data model and
its audit trail in place now means that phase can attach without redesigning the user or
membership tables.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _

from apps.core.services import log_activity, stamp_history

from .models import Location, MembershipLocationScope
from .selectors import get_descendant_ids

VERB_ADDED = "membership_scope.added"
VERB_REMOVED = "membership_scope.removed"


@transaction.atomic
def add_membership_scope(
    membership: Any, location: Location, *, include_descendants: bool = True, actor: Any = None
) -> MembershipLocationScope:
    """Record that ``membership`` is intended to cover ``location`` (idempotent)."""
    if location.organization_id != membership.organization_id:
        raise ValidationError({"location": _("The location must belong to the membership's organization.")})
    if not membership.is_active:
        raise ValidationError(_("Scope can only be assigned to an active membership."))
    scope, created = MembershipLocationScope.objects.get_or_create(
        membership=membership,
        location=location,
        defaults={"include_descendants": include_descendants},
    )
    if not created and scope.include_descendants != include_descendants:
        scope.include_descendants = include_descendants
        stamp_history(scope, actor, VERB_ADDED)
        scope.save(update_fields=["include_descendants", "updated_at"])
        created = True  # logged below as a change
    elif created:
        stamp_history(scope, actor, VERB_ADDED)
    if created:
        log_activity(
            verb=VERB_ADDED,
            organization=membership.organization,
            actor=actor,
            target=scope,
            description=f"Scoped {membership.user} to {location.name}",
            metadata={"location_id": str(location.pk), "include_descendants": include_descendants},
        )
    return scope


@transaction.atomic
def remove_membership_scope(scope: MembershipLocationScope, *, actor: Any = None) -> None:
    membership = scope.membership
    location = scope.location
    stamp_history(scope, actor, VERB_REMOVED)
    try:
        scope.delete()
    except IntegrityError as exc:  # pragma: no cover - defensive
        raise ValidationError(_("This scope cannot be removed right now.")) from exc
    log_activity(
        verb=VERB_REMOVED,
        organization=membership.organization,
        actor=actor,
        description=f"Removed scope {location.name} from {membership.user}",
        metadata={"location_id": str(location.pk)},
    )


def scope_roots(membership: Any) -> list[MembershipLocationScope]:
    """The stored scope rows of ``membership`` (what an administrator granted)."""
    return list(membership.location_scopes.select_related("location").order_by("location__name"))


def scoped_location_ids(membership: Any) -> set:
    """Every location id the stored scope would cover (roots plus, where asked, descendants).

    A *read helper only*: it expands the data, it does not enforce anything.
    """
    rows = membership.location_scopes.values_list("location_id", "include_descendants")
    ids = {location_id for location_id, _with in rows}
    subtree_roots = [location_id for location_id, with_children in rows if with_children]
    if subtree_roots:
        ids |= get_descendant_ids(subtree_roots)
    return ids
