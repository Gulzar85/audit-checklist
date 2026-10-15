"""Business operations for the identity domain.

These are the sanctioned way to provision, link, synchronise and transition a
user, and to manage organization memberships.  The authentication backend, the
web views, the API and the admin all call into here, so validation, transactions,
locking, identity-matching policy and activity logging happen exactly once.

Authorization is *not* decided here: entry points ask the policy layer
(``policies.py``) first, then call a service.  Services own the business rules
that must hold no matter who is calling.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.services import apply_changes, log_activity, stamp_history
from apps.organizations.models import OrganizationMembership

from .constants import (
    ACTIVE_ORGANIZATION_SESSION_KEY,
    IDENTITY_VERBS,
    PROVISION_ACTIVE,
    STATUS_VERBS,
    UserStatus,
)
from .entra import ExternalIdentity, tenant_is_allowed
from .preferences import PREFERENCES, validate_preference
from .exceptions import IdentityNotAllowed
from .models import UserPreference
from .selectors import get_user_by_email, get_user_by_entra_identity

User = get_user_model()
Membership = OrganizationMembership


# ---------------------------------------------------------------------------
# Policy helpers (configuration-driven, never hard-coded)
# ---------------------------------------------------------------------------
def _provisioning_status() -> str:
    """The status a brand-new external identity is provisioned with."""
    mode = getattr(settings, "AUTH_PROVISIONING_MODE", "pending")
    return UserStatus.ACTIVE if mode == PROVISION_ACTIVE else UserStatus.PENDING


def _email_linking_policy() -> str:
    return getattr(settings, "AUTH_EMAIL_LINKING", "non_staff")


def _email_linking_allowed(candidate: Any) -> bool:
    """Whether a first-time Entra identity may be linked to ``candidate`` by email.

    Default ``non_staff``: never link an external identity to a privileged local
    account, so a matching email can never inherit administrator rights
    (spec §13 / §32).  ``all`` opts in explicitly; ``disabled`` refuses.
    """
    policy = _email_linking_policy()
    if policy == "disabled":
        return False
    if policy == "all":
        return True
    return not (candidate.is_staff or candidate.is_superuser)


def _status_reason(status: str) -> str:
    return {
        UserStatus.PENDING: "account_pending",
        UserStatus.SUSPENDED: "account_suspended",
        UserStatus.INACTIVE: "account_inactive",
    }.get(status, "account_inactive")


def _denial_reason(user: Any) -> str | None:
    """The reason sign-in is refused for ``user``, or ``None`` if it is allowed.

    Returned rather than raised so the caller can commit any account change made
    during resolution (e.g. a freshly provisioned pending account) *before* the
    denial is raised.
    """
    if not user.can_sign_in:
        return _status_reason(user.status)
    return None


def _identity_fields(identity: ExternalIdentity, current: Any) -> dict[str, Any]:
    """The identity-provider-owned fields, ready for :func:`apply_changes`.

    Application-managed fields (status, memberships, staff flags) are never
    touched here.  A changed email is only applied when it is not already taken
    by another account (``old email -> new email`` without a duplicate user).
    """
    data: dict[str, Any] = {}
    if identity.display_name:
        data["display_name"] = identity.display_name
    if identity.first_name:
        data["first_name"] = identity.first_name
    if identity.last_name:
        data["last_name"] = identity.last_name
    if identity.job_title:
        data["job_title"] = identity.job_title
    if identity.tenant_id:
        data["entra_tenant_id"] = identity.tenant_id

    email = (identity.email or "").strip()
    if email and email.lower() != (current.email or "").lower():
        clash = User.objects.filter(email__iexact=email).exclude(pk=current.pk).exists()
        if not clash:
            data["email"] = email
    return data


# ---------------------------------------------------------------------------
# Provisioning / linking / synchronisation
# ---------------------------------------------------------------------------
@transaction.atomic
def provision_user(
    identity: ExternalIdentity,
    *,
    actor: Any = None,
    status: str | None = None,
) -> Any:
    """Create a new application user from a verified Entra identity.

    The user is created with an unusable local password (Microsoft holds the
    credential) and, by default, in the ``pending`` state until an administrator
    approves them.
    """
    status = status or _provisioning_status()
    user = User.objects.create_user(
        email=identity.email,
        entra_object_id=identity.object_id,
        entra_tenant_id=identity.tenant_id,
        display_name=identity.display_name,
        first_name=identity.first_name,
        last_name=identity.last_name,
        job_title=identity.job_title,
        status=status,
    )
    stamp_history(user, actor, IDENTITY_VERBS["user_created"])
    log_activity(
        verb=IDENTITY_VERBS["user_created"],
        actor=actor,
        target=user,
        description=f"Provisioned {user.email} from Microsoft Entra ID",
        metadata={"source": "entra", "status": status},
    )
    return user


@transaction.atomic
def link_external_identity(user: Any, identity: ExternalIdentity, *, actor: Any = None) -> Any:
    """Link an existing Django user to a verified Entra identity.

    Refuses when the account is already linked to a *different* object id, which
    is what prevents a mismatched identity from stealing an account.
    """
    current = User.objects.select_for_update().get(pk=user.pk)
    if current.entra_object_id and current.entra_object_id != identity.object_id:
        raise IdentityNotAllowed("identity_conflict", user=current)

    data = _identity_fields(identity, current)
    data["entra_object_id"] = identity.object_id
    changes = apply_changes(current, data)
    current.save()
    stamp_history(current, actor, IDENTITY_VERBS["identity_linked"])
    log_activity(
        verb=IDENTITY_VERBS["identity_linked"],
        actor=actor,
        target=current,
        description=f"Linked Microsoft Entra identity to {current.email}",
        metadata={"object_id": identity.object_id, "changes": changes},
    )
    return current


@transaction.atomic
def unlink_external_identity(user: Any, *, actor: Any = None, reason: str = "") -> Any:
    """Detach the Entra identity from ``user`` (administrative; audited).

    The account keeps its profile, memberships and history; it simply stops being
    reachable through that Microsoft identity.  To avoid locking a person out for good,
    a user left with *no* way to sign in must be an administrator-recoverable state:
    the account is moved to ``inactive`` unless it still has a usable local password.
    """
    current = User.objects.select_for_update().get(pk=user.pk)
    if not current.entra_object_id:
        raise ValidationError(_("This account is not linked to a Microsoft identity."))
    previous = {"object_id": current.entra_object_id, "tenant_id": current.entra_tenant_id}
    current.entra_object_id = None
    current.entra_tenant_id = ""
    deactivated = False
    if current.status == UserStatus.ACTIVE and not current.has_usable_password():
        current.status = UserStatus.INACTIVE
        deactivated = True
    stamp_history(current, actor, IDENTITY_VERBS["identity_unlinked"])
    current.save()
    log_activity(
        verb=IDENTITY_VERBS["identity_unlinked"],
        actor=actor,
        target=current,
        description=f"Unlinked Microsoft Entra identity from {current.email}",
        metadata={**previous, "reason": reason[:200], "deactivated": deactivated},
    )
    return current


@transaction.atomic
def sync_user_identity(user: Any, identity: ExternalIdentity, *, actor: Any = None) -> Any:
    """Refresh the identity-provider-owned fields of a linked user.

    Application-managed state (status, memberships, roles, scope) is untouched.
    """
    current = User.objects.select_for_update().get(pk=user.pk)
    changes = apply_changes(current, _identity_fields(identity, current))
    if not changes:
        return current
    current.save()
    stamp_history(current, actor, IDENTITY_VERBS["user_updated"])
    log_activity(
        verb=IDENTITY_VERBS["user_updated"],
        actor=actor,
        target=current,
        description=f"Synced profile for {current.email} from Microsoft Entra ID",
        metadata={"source": "entra", "changes": changes},
    )
    return current


@transaction.atomic
def _resolve_entra_user(
    identity: ExternalIdentity, *, actor: Any = None
) -> tuple[Any, bool, str | None]:
    """Resolve the mapping *inside* a transaction; report, never raise, a denial.

    Returns ``(user, created, denial_reason)``. Only validation/conflict errors
    that change nothing are raised here; lifecycle denials are returned so the
    outer :func:`resolve_entra_user` can raise them after the commit.
    """
    if not identity.object_id:
        raise IdentityNotAllowed("invalid_identity")
    if not tenant_is_allowed(identity.tenant_id):
        raise IdentityNotAllowed("tenant_not_allowed")
    if not (identity.email or "").strip():
        raise IdentityNotAllowed("missing_email")

    # 1. Already linked?  ``entra_object_id`` is globally unique.
    linked = get_user_by_entra_identity(identity.object_id)
    if linked is not None:
        if (
            linked.entra_tenant_id
            and identity.tenant_id
            and linked.entra_tenant_id != identity.tenant_id
        ):
            raise IdentityNotAllowed("tenant_not_allowed", user=linked)
        if linked.can_sign_in:
            linked = sync_user_identity(linked, identity, actor=actor)
        return linked, False, _denial_reason(linked)

    # 2. Known email -> controlled linking.
    candidate = get_user_by_email(identity.email)
    if candidate is not None:
        if candidate.entra_object_id and candidate.entra_object_id != identity.object_id:
            raise IdentityNotAllowed("identity_conflict", user=candidate)
        if not _email_linking_allowed(candidate):
            raise IdentityNotAllowed("identity_conflict", user=candidate)
        linked = link_external_identity(candidate, identity, actor=actor)
        return linked, False, _denial_reason(linked)

    # 3. Unknown -> provision (pending by default).
    provisioned = provision_user(identity, actor=actor)
    return provisioned, True, _denial_reason(provisioned)


def resolve_entra_user(identity: ExternalIdentity, *, actor: Any = None) -> tuple[Any, bool]:
    """Find, link or provision the Django user for a verified Entra identity.

    Returns ``(user, created)``.  Raises :class:`IdentityNotAllowed` when the
    identity maps to an account that may not sign in (tenant not allowed,
    identity conflict, pending/suspended/inactive).  Authentication success at
    Entra never overrides the application's own lifecycle decision.

    The denial is raised *after* the work commits, so a newly provisioned
    (pending) or newly linked account is persisted for an administrator to
    approve or reactivate rather than being rolled back with the exception.
    """
    user, created, denial = _resolve_entra_user(identity, actor=actor)
    if denial is not None:
        raise IdentityNotAllowed(denial, user=user)
    return user, created


# ---------------------------------------------------------------------------
# User lifecycle
# ---------------------------------------------------------------------------
@transaction.atomic
def set_user_status(user: Any, status: str, *, actor: Any = None) -> Any:
    """Transition a user's lifecycle state and log the business event."""
    if status not in UserStatus.values:
        raise ValidationError({"status": _("Unknown status “%(status)s”.") % {"status": status}})

    current = User.objects.select_for_update().get(pk=user.pk)
    if current.status == status:
        return current

    previous = current.status
    current.status = status
    verb = STATUS_VERBS.get(status, IDENTITY_VERBS["user_updated"])
    stamp_history(current, actor, verb)
    current.save(update_fields=["status"])
    log_activity(
        verb=verb,
        actor=actor,
        target=current,
        description=f"User {current.email} is now {status}",
        metadata={"from": previous, "to": status},
    )
    return current


def activate_user(user: Any, *, actor: Any = None) -> Any:
    return set_user_status(user, UserStatus.ACTIVE, actor=actor)


def suspend_user(user: Any, *, actor: Any = None) -> Any:
    return set_user_status(user, UserStatus.SUSPENDED, actor=actor)


def deactivate_user(user: Any, *, actor: Any = None) -> Any:
    return set_user_status(user, UserStatus.INACTIVE, actor=actor)


# ---------------------------------------------------------------------------
# Organization membership
# ---------------------------------------------------------------------------
def _clear_other_defaults(user: Any, organization: Any) -> None:
    OrganizationMembership.objects.filter(user=user, is_default=True).exclude(
        organization_id=getattr(organization, "pk", organization)
    ).update(is_default=False)


@transaction.atomic
def add_membership(
    user: Any,
    organization: Any,
    *,
    actor: Any = None,
    is_default: bool = False,
    is_active: bool = True,
) -> OrganizationMembership:
    """Grant ``user`` membership of ``organization`` (idempotent).

    A user who previously *left* is re-admitted by reusing the same row (it keeps its
    history); the event is logged as ``organization_membership.created`` with
    ``rejoined: true``.
    """
    status = Membership.Status.ACTIVE if is_active else Membership.Status.SUSPENDED
    membership, created = OrganizationMembership.objects.get_or_create(
        user=user,
        organization=organization,
        defaults={"is_default": is_default, "status": status},
    )
    rejoined = False
    if not created and membership.status == Membership.Status.LEFT:
        membership = OrganizationMembership.objects.select_for_update().get(pk=membership.pk)
        membership.status = status
        membership.is_default = is_default
        membership.save()
        rejoined = True
    if created or rejoined:
        if is_default:
            _clear_other_defaults(user, organization)
        log_activity(
            verb=IDENTITY_VERBS["membership_created"],
            organization=organization,
            actor=actor,
            target=membership,
            description=f"Added {user} to {organization}",
            metadata={"is_default": is_default, "status": membership.status, "rejoined": rejoined},
        )
    return membership


_MEMBERSHIP_STATUS_VERBS = {
    Membership.Status.ACTIVE: IDENTITY_VERBS["membership_updated"],
    Membership.Status.SUSPENDED: IDENTITY_VERBS["membership_suspended"],
    Membership.Status.LEFT: IDENTITY_VERBS["membership_removed"],
}


@transaction.atomic
def set_membership_status(
    membership: OrganizationMembership, status: str, *, actor: Any = None
) -> OrganizationMembership:
    """Move a membership between ``active``, ``suspended`` and ``left`` (audited).

    Nothing is deleted: a membership that is not active simply stops granting access
    (the tenant choke point filters on ``is_active``), and ``left_at`` records when the
    user left.
    """
    if status not in Membership.Status.values:
        raise ValidationError({"status": _("Unknown membership status “%(status)s”.") % {"status": status}})
    current = OrganizationMembership.objects.select_for_update().select_related("organization", "user").get(pk=membership.pk)
    if current.status == status:
        return current
    previous = current.status
    current.status = status
    stamp_history(current, actor, _MEMBERSHIP_STATUS_VERBS[status])
    current.save()
    log_activity(
        verb=_MEMBERSHIP_STATUS_VERBS[status],
        organization=current.organization,
        actor=actor,
        target=current,
        description=f"Membership of {current.user} in {current.organization} is now {status}",
        metadata={"from": previous, "to": status, "user_id": str(current.user_id)},
    )
    return current


def set_membership_active(membership: OrganizationMembership, is_active: bool, *, actor: Any = None) -> OrganizationMembership:
    """Compatibility wrapper: ``True`` -> active, ``False`` -> suspended."""
    return set_membership_status(
        membership, Membership.Status.ACTIVE if is_active else Membership.Status.SUSPENDED, actor=actor
    )


@transaction.atomic
def set_default_organization(user: Any, organization: Any, *, actor: Any = None) -> OrganizationMembership:
    """Make ``organization`` the user's default (creating the membership if needed)."""
    membership = OrganizationMembership.objects.filter(
        user=user, organization=organization
    ).first()
    if membership is None:
        return add_membership(user, organization, actor=actor, is_default=True)
    _clear_other_defaults(user, organization)
    if not membership.is_default:
        membership.is_default = True
        membership.save(update_fields=["is_default", "updated_at"])
        log_activity(
            verb=IDENTITY_VERBS["membership_updated"],
            organization=organization,
            actor=actor,
            target=membership,
            description=f"Set {organization} as the default organization for {user}",
        )
    return membership


def remove_membership(membership: OrganizationMembership, *, actor: Any = None) -> OrganizationMembership:
    """The user *leaves* the organization: the row is kept (``status=left``, ``left_at``)."""
    return set_membership_status(membership, Membership.Status.LEFT, actor=actor)


# ---------------------------------------------------------------------------
# Organization context (request-scoped)
# ---------------------------------------------------------------------------
def set_active_organization(request: Any, organization: Any, *, actor: Any = None) -> None:
    """Record the request's active organization in the session and log it."""
    request.session[ACTIVE_ORGANIZATION_SESSION_KEY] = str(organization.pk)
    log_activity(
        verb=IDENTITY_VERBS["organization_switched"],
        organization=organization,
        actor=actor or getattr(request, "user", None),
        description=f"Switched to organization {organization.name}",
        metadata={"organization_id": str(organization.pk)},
    )


def clear_active_organization(request: Any) -> None:
    request.session.pop(ACTIVE_ORGANIZATION_SESSION_KEY, None)


# ---------------------------------------------------------------------------
# Authentication audit
# ---------------------------------------------------------------------------
def record_logout(*, user: Any = None, actor: Any = None) -> None:
    """Record a sign-out event."""
    log_activity(
        verb=IDENTITY_VERBS["logout"],
        actor=actor or user,
        target=user,
        description="Signed out",
    )


def log_profile_update(*, actor: Any = None, target: Any = None) -> None:
    """Record a self-service profile edit."""
    log_activity(
        verb=IDENTITY_VERBS["user_updated"],
        actor=actor,
        target=target or actor,
        description="Updated own profile",
        metadata={"source": "self_service"},
    )


# ---------------------------------------------------------------------------
# Authentication audit
# ---------------------------------------------------------------------------
def record_authentication(
    *,
    user: Any = None,
    actor: Any = None,
    provider: str = "entra",
    success: bool = True,
    reason: str = "",
    organization: Any = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write an authentication success/failure event to the activity log.

    Never records tokens, secrets or raw claims - only the reason code.
    """
    verb = IDENTITY_VERBS["auth_success"] if success else IDENTITY_VERBS["auth_failure"]
    payload = {"provider": provider}
    if reason:
        payload["reason"] = reason
    if metadata:
        payload.update(metadata)
    log_activity(
        verb=verb,
        organization=organization,
        actor=actor or (user if success else None),
        target=user,
        description=f"Microsoft Entra sign-in {'succeeded' if success else 'failed'}",
        metadata=payload,
    )


# ---------------------------------------------------------------------------
# User preferences (allow-listed; appearance belongs to the theme engine)
# ---------------------------------------------------------------------------
@transaction.atomic
def set_preference(user: Any, key: str, value: Any, *, actor: Any = None) -> Any:
    """Set (or, with ``None``/``""``, clear) one validated preference; audited on change."""
    if key not in PREFERENCES:
        raise ValidationError({key: _("Unknown preference.")})
    existing = UserPreference.objects.select_for_update().filter(user=user, key=key).first()
    if value in (None, ""):
        if existing is None:
            return None
        previous = existing.value
        existing.delete()
        new = None
    else:
        new = validate_preference(key, value)
        previous = existing.value if existing else None
        if previous == new:
            return existing
        if existing:
            existing.value = new
            existing.save(update_fields=["value", "updated_at"])
        else:
            existing = UserPreference.objects.create(user=user, key=key, value=new)
    log_activity(
        verb=IDENTITY_VERBS["preference_changed"],
        actor=actor or user,
        target=user,
        description=f"Preference {key} changed",
        metadata={"key": key, "from": previous, "to": new},
    )
    return existing if new is not None else None
