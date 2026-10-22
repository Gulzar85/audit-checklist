"""Test settings.

PostgreSQL is used for the integration test suite (matching production
semantics) unless ``TEST_DATABASE_URL`` points somewhere else.  The
configuration is optimised for speed: a fast password hasher and the in-memory
email backend.
"""

from __future__ import annotations

from .base import *  # noqa: F401,F403
from .base import BASE_CSP
from .env import database_from_url, env

DEBUG = False

# Evidence tests write to a throw-away private directory (tests/evidence_support.py swaps in scanner doubles per test).
import tempfile as _tempfile  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

EVIDENCE_ROOT = _Path(_tempfile.mkdtemp(prefix="evidence-tests-"))
EVIDENCE_SCAN_REQUIRED = False

# Reuse the development database URL unless a dedicated test database is given.
DATABASES = {
    "default": database_from_url(
        env("TEST_DATABASE_URL") or env("DATABASE_URL"),
        conn_max_age=0,
        conn_health_checks=False,
    )
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# The Django test runner overrides MAILERS with the memory backend; declaring it
# here keeps behaviour explicit and avoids the deprecation fallback.
MAILERS = {
    "default": {
        "BACKEND": "django.core.mail.backends.locmem.EmailBackend",
        "OPTIONS": {},
    },
}

# Enforce CSP during tests so header presence is a hard assertion.
SECURE_CSP = BASE_CSP

# Keep the test suite fast and deterministic.
TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}

LOGGING["root"]["level"] = "WARNING"  # type: ignore[name-defined]  # noqa: F405
