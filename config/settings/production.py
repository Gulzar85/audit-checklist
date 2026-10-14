"""Production settings.

Every value here is driven by the environment; the file contains no secrets.
"""

from __future__ import annotations

from .base import *  # noqa: F401,F403
from .base import BASE_CSP, MIDDLEWARE, STORAGES
from .env import env_bool, env_int, env_list

DEBUG = False

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
