"""Production settings.

Every value here is driven by the environment; the file contains no secrets.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import BASE_CSP, MIDDLEWARE, STORAGES
from .env import env_bool, env_int, env_list

DEBUG = False

# The local email/password fallback must never be reachable in production: Microsoft Entra ID
# is the only production identity provider.  Refuse to start rather than run exposed.
if ENABLE_PASSWORD_LOGIN:  # noqa: F405
    raise ImproperlyConfigured(
        "ENABLE_PASSWORD_LOGIN must be false in production (Microsoft Entra ID is the identity provider)."
    )

# Hosts / origins must be supplied explicitly in production.
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", [])
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", [])

# ---------------------------------------------------------------------------
# Security hardening
# ---------------------------------------------------------------------------
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", True)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", True)

# Enforced CSP.
SECURE_CSP = BASE_CSP

# ---------------------------------------------------------------------------
# Static files served by WhiteNoise
# ---------------------------------------------------------------------------
MIDDLEWARE = [*MIDDLEWARE]
MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
STORAGES = {
    **STORAGES,
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# ---------------------------------------------------------------------------
# Production mailer (Microsoft 365 SMTP / Graph later)
# ---------------------------------------------------------------------------
# MAILERS inherit the SMTP configuration from base settings.
