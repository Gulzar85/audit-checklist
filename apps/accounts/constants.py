"""Identity vocabulary shared by the accounts app.

Kept import-light (no models, no app registry access) so it can be imported from
settings, migrations, the API schema and templates without side effects.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class UserStatus(models.TextChoices):
    """The authoritative application lifecycle state of a user.

    Django's ``is_active`` remains the boolean authentication gate that
    ``ModelBackend`` consults; it is kept in lock-step with this field by
    :class:`~apps.accounts.models.User` (``is_active`` is a stored mirror).  The
    distinction matters because the four states are *not* interchangeable:
    ``PENDING`` (provisioned, awaiting approval), ``SUSPENDED`` and ``INACTIVE``
    all mean "cannot sign in", but an administrator needs to tell them apart.
    """

    PENDING = "pending", _("Pending")
    ACTIVE = "active", _("Active")
    SUSPENDED = "suspended", _("Suspended")
    INACTIVE = "inactive", _("Inactive")


#: The states that permit normal application use.
SIGNABLE_IN_STATUSES = (UserStatus.ACTIVE,)

# --- First-login provisioning -------------------------------------------------
#: New Entra identities are created ``pending`` and must be approved.
PROVISION_PENDING = "pending"
#: New Entra identities are created ``active`` (only for trusted single-tenant
#: deployments where mere presence in the tenant is the authorisation).
PROVISION_ACTIVE = "active"
PROVISION_MODES = (PROVISION_PENDING, PROVISION_ACTIVE)

# --- Session keys -------------------------------------------------------------
#: The organization the user is currently acting within (validated per request).
ACTIVE_ORGANIZATION_SESSION_KEY = "active_organization_id"
#: The in-flight MSAL authorization-code flow (state/nonce/PKCE), server-side only.
OIDC_FLOW_SESSION_KEY = "microsoft_auth_flow"
#: Where to send the user after a successful sign-in (validated to stay internal).
POST_LOGIN_REDIRECT_SESSION_KEY = "post_login_redirect"

# --- Activity-log verbs -------------------------------------------------------
#: Reused verb strings for the business activity log (apps.core.services.log_activity).
IDENTITY_VERBS = {
    "user_created": "user.created",
    "user_updated": "user.updated",
    "user_activated": "user.activated",
    "user_suspended": "user.suspended",
    "user_deactivated": "user.deactivated",
    "identity_linked": "identity.linked",
    "identity_unlinked": "identity.unlinked",
    "membership_created": "organization_membership.created",
    "membership_updated": "organization_membership.updated",
    "membership_suspended": "organization_membership.suspended",
    "membership_removed": "organization_membership.removed",
    "auth_success": "authentication.success",
    "auth_failure": "authentication.failure",
    "preference_changed": "user.preference_changed",
    "logout": "logout",
    "organization_switched": "organization.switched",
}

#: Lifecycle verb per target status (activate/suspend/deactivate).
STATUS_VERBS = {
    UserStatus.ACTIVE: IDENTITY_VERBS["user_activated"],
    UserStatus.SUSPENDED: IDENTITY_VERBS["user_suspended"],
    UserStatus.INACTIVE: IDENTITY_VERBS["user_deactivated"],
}
