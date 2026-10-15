"""Identity-domain exceptions.

These carry a stable ``reason`` code so the authentication layer can map a
failure to a safe, user-facing message *and* a structured audit entry without
ever leaking internal detail (spec §28).
"""

from __future__ import annotations

from typing import Any


class IdentityError(Exception):
    """Base class for identity failures."""


class IdentityNotAllowed(IdentityError):
    """A verified external identity may not be granted application access.

    ``reason`` is one of the codes in :data:`REASON_MESSAGES`.  ``user`` is the
    (possibly newly provisioned) application user, when one exists.
    """

    def __init__(self, reason: str, *, user: Any = None, message: str = "") -> None:
        self.reason = reason
        self.user = user
        self.message = message or reason
        super().__init__(self.message)


#: Safe, deliberately-vague messages shown to the user (spec §28).
REASON_MESSAGES = {
    "invalid_identity": "We could not verify your sign-in details.",
    "missing_email": "Your account has no email address we can use. Contact your administrator.",
    "tenant_not_allowed": "Your organization is not permitted to sign in to this application.",
    "identity_conflict": (
        "An account already exists for this email address but is not linked to your "
        "Microsoft identity. Contact your administrator to link it."
    ),
    "account_pending": "Your account is awaiting administrator approval.",
    "account_suspended": "Your account has been suspended. Contact your administrator.",
    "account_inactive": "Your account is inactive. Contact your administrator.",
    "rate_limited": "Too many sign-in attempts. Please wait a few minutes and try again.",
    # Provider / flow failures: deliberately generic, never echoing what Microsoft sent.
    "invalid_state": "Your sign-in attempt could not be verified or has expired. Please start again.",
    "authentication_failed": "We could not sign you in. Please try again.",
    "access_denied": "Sign-in was cancelled or access was not granted.",
    "invalid_request": "The sign-in request was not valid. Please start again.",
    "invalid_client": "Sign-in is temporarily unavailable. Contact your administrator.",
    "membership_missing": "Your account is not a member of any organization yet. Contact your administrator.",
    "user_not_allowed": "You are not permitted to sign in to this application.",
}
