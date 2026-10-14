"""Authorization and business-policy checks for the core app."""

from __future__ import annotations

from typing import Any


def is_platform_staff(user: Any) -> bool:
    """Return ``True`` if the user may access internal platform tooling."""
    return bool(getattr(user, "is_authenticated", False) and getattr(user, "is_staff", False))
