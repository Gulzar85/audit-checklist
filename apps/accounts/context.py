"""Request-scoped identity helpers.

The active organization is resolved *lazily* (only when something actually asks
for it) so that requests which never render the organization switcher - the REST
API above all - pay nothing for it.  Resolution is memoised on the request.
"""

from __future__ import annotations

from typing import Any

from .constants import ACTIVE_ORGANIZATION_SESSION_KEY
from .selectors import get_active_memberships, resolve_active_organization


def _signable(user: Any) -> bool:
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and getattr(user, "can_sign_in", False)
    )


def get_active_organization(request: Any) -> Any | None:
    """Return (and memoise) the request's active organization, or ``None``.

    The session value is validated through the organization selector; a stale or
    forged id falls back to the user's default organization, never to the id
    itself.
    """
    if getattr(request, "_organization_loaded", False):
        return getattr(request, "organization", None)
    request._organization_loaded = True
    request.organization = None

    user = getattr(request, "user", None)
    if not _signable(user):
        return None

    organization_id = None
    if hasattr(request, "session"):
        organization_id = request.session.get(ACTIVE_ORGANIZATION_SESSION_KEY)

    organization = resolve_active_organization(user, organization_id)
    request.organization = organization
    if organization is not None and hasattr(request, "session"):
        # Persist the resolved choice so later requests take the fast path.  Only
        # write when it actually changes: an unconditional write would mark the
        # session modified and add a query to every authenticated request.
        resolved = str(organization.pk)
        if organization_id != resolved:
            request.session[ACTIVE_ORGANIZATION_SESSION_KEY] = resolved
    return organization


def get_organization_memberships(request: Any) -> list[Any]:
    """Return the caller's active memberships (for the switcher)."""
    user = getattr(request, "user", None)
    if not _signable(user):
        return []
    return list(get_active_memberships(user))
