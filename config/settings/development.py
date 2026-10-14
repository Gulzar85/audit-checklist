"""Development settings."""

from __future__ import annotations

from .base import *  # noqa: F401,F403
from .base import BASE_CSP, INSTALLED_APPS, REST_FRAMEWORK

DEBUG = True

ALLOWED_HOSTS = ["*"]

# Local development should not send real email and must not require SMTP
# credentials. Console output keeps verification easy.
MAILERS = {
    "default": {
        "BACKEND": "django.core.mail.backends.console.EmailBackend",
        "OPTIONS": {},
    },
}

# Report-only CSP while the UI is being built: violations are surfaced in the
# browser console without breaking the application. Enforcement is enabled in
# production.
SECURE_CSP_REPORT_ONLY = BASE_CSP

# Browsable API is helpful locally; the JSON envelope remains the contract.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_RENDERER_CLASSES": [
        "apps.api.renderers.EnvelopeJSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

INSTALLED_APPS = [*INSTALLED_APPS, "django.contrib.admindocs"]
