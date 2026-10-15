"""Cache-backed rate limiting for the credential endpoints.

Sign-in is where an attacker would try to brute-force a password or replay an
authorization code, so those two endpoints are throttled.  The rate is kept in
the project's configured cache (Django's default: an in-process ``LocMemCache``)
- Phase 2 adds no new infrastructure such as Redis, and a deployment that needs
a shared counter can point ``CACHES`` at any backend without touching this code.

Only *failed* attempts count, and a successful sign-in clears the counter, so a
legitimate user is never locked out by their own valid credentials.  The limits
are configuration-driven (spec §31: no hard-coded policy).
"""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest

#: Scope names keep the password and Microsoft counters independent.
PASSWORD_SCOPE = "password"
MICROSOFT_SCOPE = "microsoft"


def _max_attempts() -> int:
    return int(getattr(settings, "AUTH_THROTTLE_MAX_ATTEMPTS", 10))


def _window_seconds() -> int:
    return int(getattr(settings, "AUTH_THROTTLE_WINDOW_SECONDS", 300))


def client_address(request: HttpRequest) -> str:
    """The caller's address, without trusting a header the caller can forge.

    ``X-Forwarded-For`` is a list each proxy appends to; a client can pre-fill the *left*
    side.  Only the entries added by *our* proxies (``TRUSTED_PROXY_COUNT`` from the right)
    are believable, so the address is read that many hops from the end.  With no trusted
    proxy (the default) the header is ignored and ``REMOTE_ADDR`` is used.
    """
    remote = request.META.get("REMOTE_ADDR", "")
    hops = int(getattr(settings, "TRUSTED_PROXY_COUNT", 0))
    if hops <= 0:
        return remote
    forwarded = [part.strip() for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if part.strip()]
    if len(forwarded) < hops:
        return remote  # fewer hops than expected: the header is not from our proxies
    return forwarded[-hops]


def _client_identifier(request: HttpRequest) -> str:
    """A stable, privacy-preserving key for the caller (the address is hashed)."""
    return hashlib.sha256(client_address(request).encode("utf-8")).hexdigest()


def _cache_key(request: HttpRequest, scope: str) -> str:
    return f"auth-throttle:{scope}:{_client_identifier(request)}"


def is_blocked(request: HttpRequest, scope: str) -> bool:
    """Whether the caller has exhausted its attempts for ``scope``."""
    return cache.get(_cache_key(request, scope), 0) >= _max_attempts()


def record_failure(request: HttpRequest, scope: str) -> None:
    """Count one failed attempt, starting a fixed window on the first one."""
    key = _cache_key(request, scope)
    window = _window_seconds()
    if cache.add(key, 1, window):
        return
    try:
        cache.incr(key)
    except ValueError:  # the key expired between ``get`` and ``incr``
        cache.set(key, 1, window)


def clear(request: HttpRequest, scope: str) -> None:
    """Reset the counter after a successful sign-in."""
    cache.delete(_cache_key(request, scope))
