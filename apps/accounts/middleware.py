"""Identity middleware.

Its one job is to enforce the user lifecycle on **every** request - including
requests that never touch a view's own policy checks.  A user suspended or
deactivated while their session is still alive is signed out immediately:
authentication success at Microsoft Entra ID never overrides the application's
own lifecycle decision (spec §15).

Resolving the active organization is deliberately *not* done here; it is lazy
(see :mod:`apps.accounts.context`) so the REST API pays nothing for it.
"""

from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth import logout

from .constants import ACTIVE_ORGANIZATION_SESSION_KEY

logger = logging.getLogger("apps.accounts")


class IdentityMiddleware:
    """Force a sign-out when an authenticated user may no longer sign in."""

    def __init__(self, get_response):  # noqa: ANN001
        self.get_response = get_response

    def __call__(self, request):  # noqa: ANN001
        request.organization = None
        request._organization_loaded = False

        user = getattr(request, "user", None)
        if (
            user is not None
            and getattr(user, "is_authenticated", False)
            and not getattr(user, "can_sign_in", True)
        ):
            logger.info("Forcing sign-out for non-active user %s (%s)", user.pk, user.status)
            if hasattr(request, "session"):
                request.session.pop(ACTIVE_ORGANIZATION_SESSION_KEY, None)
            logout(request)
            try:
                messages.warning(
                    request, "Your session ended because your account is no longer active."
                )
            except Exception:  # pragma: no cover - messages backend unavailable
                pass

        return self.get_response(request)
