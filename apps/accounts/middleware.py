"""Identity middleware.

Two jobs, both enforced on **every** request - including ones that never touch a view's own
policy checks:

1. **Lifecycle.**  A user suspended or deactivated while their session is still alive is
   signed out immediately: authentication success at Microsoft Entra ID never overrides the
   application's own lifecycle decision (spec §15).
2. **Session lifetime (ADR 0038).**  A sign-in ends after ``SESSION_ABSOLUTE_TIMEOUT_SECONDS``
   whatever the activity, and after ``SESSION_IDLE_TIMEOUT_SECONDS`` without a request.  The
   idle stamp is written at most once a minute, so the session is not rewritten on every
   request.  When a session has expired, API / HTMX / ``Accept: application/json`` callers get a
   ``401`` JSON ``session_expired`` (HTMX also gets ``HX-Redirect``), and page requests are signed
   out and shown a clear message by the view's normal login redirect - no redirect loop.

Resolving the active organization is deliberately *not* done here; it is lazy
(see :mod:`apps.accounts.context`) so the REST API pays nothing for it.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.http import HttpRequest, JsonResponse
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from .constants import ACTIVE_ORGANIZATION_SESSION_KEY

logger = logging.getLogger("apps.accounts")

#: Session keys holding the sign-in start and the last (coarse) activity time, in epoch seconds.
AUTH_STARTED_KEY = "auth_started_at"
LAST_ACTIVITY_KEY = "last_activity_at"
#: The idle stamp is refreshed at most this often.
ACTIVITY_WRITE_INTERVAL = 60

#: The "am I still signed in?" probe.  It reads the session but does not extend it, so a page
#: or PWA polling it cannot keep an idle session alive.
SESSION_PROBE_PATH = "/auth/session/"

SESSION_EXPIRED_CODE = "session_expired"
SESSION_EXPIRED_MESSAGE = "Your session expired. Please sign in again."


def wants_json(request: HttpRequest) -> bool:
    """Whether the caller expects machine-readable errors (API, HTMX, fetch)."""
    return (
        request.path.startswith("/api/")
        or request.headers.get("HX-Request") == "true"
        or "application/json" in request.headers.get("Accept", "")
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )


def _login_url(request: HttpRequest) -> str:
    target = request.get_full_path()
    url = reverse("accounts:login")
    if url_has_allowed_host_and_scheme(target, {request.get_host()}, request.is_secure()):
        return f"{url}?{urlencode({'next': target, 'expired': 1})}"
    return f"{url}?expired=1"


def _expired_reason(request: HttpRequest, now: float, *, touch: bool = True) -> str | None:
    """``"absolute"`` / ``"idle"`` when the session is over its limit, else ``None``."""
    session = request.session
    started = session.get(AUTH_STARTED_KEY)
    if started is None:  # a session that predates this feature: start counting now
        session[AUTH_STARTED_KEY] = now
        session[LAST_ACTIVITY_KEY] = now
        return None
    absolute = int(getattr(settings, "SESSION_ABSOLUTE_TIMEOUT_SECONDS", 0))
    if absolute and now - started > absolute:
        return "absolute"
    idle = int(getattr(settings, "SESSION_IDLE_TIMEOUT_SECONDS", 0))
    last = session.get(LAST_ACTIVITY_KEY, started)
    if idle and now - last > idle:
        return "idle"
    if touch and now - last >= ACTIVITY_WRITE_INTERVAL:
        session[LAST_ACTIVITY_KEY] = now
    return None


class IdentityMiddleware:
    """Enforce the user lifecycle and the session lifetime on every request."""

    def __init__(self, get_response):  # noqa: ANN001
        self.get_response = get_response

    def __call__(self, request):  # noqa: ANN001
        request.organization = None
        request._organization_loaded = False

        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False) and hasattr(request, "session"):
            if not getattr(user, "can_sign_in", True):
                logger.info("Forcing sign-out for non-active user %s (%s)", user.pk, user.status)
                request.session.pop(ACTIVE_ORGANIZATION_SESSION_KEY, None)
                logout(request)
                self._flash(request, "Your session ended because your account is no longer active.")
            else:
                reason = _expired_reason(request, time.time(), touch=request.path != SESSION_PROBE_PATH)
                if reason is not None:
                    logger.info("Session for user %s expired (%s)", user.pk, reason)
                    logout(request)
                    if wants_json(request):
                        return self._expired_json(request)
                    self._flash(request, SESSION_EXPIRED_MESSAGE)

        return self.get_response(request)

    @staticmethod
    def _flash(request: HttpRequest, text: str) -> None:
        try:
            messages.warning(request, text)
        except Exception:  # pragma: no cover - messages backend unavailable
            pass

    @staticmethod
    def _expired_json(request: HttpRequest) -> JsonResponse:
        response = JsonResponse(
            {"error": {"code": SESSION_EXPIRED_CODE, "message": SESSION_EXPIRED_MESSAGE, "details": {}}},
            status=401,
        )
        if request.headers.get("HX-Request") == "true":
            response["HX-Redirect"] = _login_url(request)
        response["Cache-Control"] = "no-store"
        return response
