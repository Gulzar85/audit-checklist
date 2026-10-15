"""Authorization and business-policy checks for the core app."""

from __future__ import annotations

from typing import Any


def is_active_user(user: Any) -> bool:
    """Return ``True`` for an authenticated, active user."""
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
    )


def is_platform_staff(user: Any) -> bool:
    """Return ``True`` if the user may access internal platform tooling.

    Platform staff are the operators of the platform itself; they are not tied
    to one tenant, so tenant-scoped selectors treat them as seeing every
    organization.  Ordinary users are always limited to their memberships.
    """
    return bool(
        is_active_user(user) and getattr(user, "is_staff", False)
    )
