"""Authentication backend for Microsoft Entra ID.

The OAuth/OpenID exchange and ID-token validation happen in the sign-in view via
MSAL (see :mod:`apps.accounts.entra`); this backend receives *already verified*
claims.  Its only responsibility is to resolve those claims to a Django user
(provision / link / synchronise) and to refuse identities the application does
not admit.

Authentication and authorization stay separate: a successful match sets
``user.backend`` so ``django.contrib.auth.login`` works, but grants no
permissions by itself.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model

from . import services
from .entra import ExternalIdentity, claims_to_identity
from .exceptions import IdentityNotAllowed


class EntraOIDCBackend:
    """Resolve a verified Entra identity to an active Django user."""

    def authenticate(  # noqa: ANN401
        self,
        request: Any,
        msal_identity: ExternalIdentity | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any | None:
        if msal_identity is None:
            return None  # not our credential type (e.g. the password form)
        if isinstance(msal_identity, dict):
            msal_identity = claims_to_identity(msal_identity)
        if not isinstance(msal_identity, ExternalIdentity):
            return None
        try:
            user, _created = services.resolve_entra_user(msal_identity)
        except IdentityNotAllowed:
            # Propagate so the sign-in view can map the reason to a safe message
            # and record an audit event; Django's ``authenticate`` lets it bubble.
            raise
        return user

    def get_user(self, user_id: Any) -> Any | None:
        User = get_user_model()
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
