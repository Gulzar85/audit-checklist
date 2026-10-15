"""Expose identity/session context to templates.

Keeps templates free of business logic: the header reads ``current_organization``
and the sign-in screen reads the availability flags.  Only the *validated* active
organization is resolved here (and only for signed-in users), so an anonymous
page costs nothing.  Richer collections (memberships, organization lists) are
provided by the views that actually display them, to keep the per-request query
count as low as possible.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest

from .context import get_active_organization


def identity_context(request: HttpRequest) -> dict[str, Any]:
    return {
        "password_login_enabled": getattr(settings, "ENABLE_PASSWORD_LOGIN", True),
        "microsoft_login_enabled": getattr(settings, "MICROSOFT_AUTH_CONFIGURED", False),
        "entra_authority": getattr(settings, "MICROSOFT_AUTHORITY", ""),
        "current_organization": get_active_organization(request),
    }
