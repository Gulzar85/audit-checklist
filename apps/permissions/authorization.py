"""Authorization service boundary.

These functions are the *only* sanctioned way for views, API endpoints and
templates to ask "may this user do this?".  Phase 0 implements a conservative
baseline (an active, authenticated user) and documents where richer,
object-level policy will plug in.  Business code should call these rather than
inspecting roles or Django permissions directly.
"""

from __future__ import annotations

from typing import Any

from .selectors import active_role_slugs_for_user


def _is_active_user(user: Any) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False) and getattr(user, "is_active", False))


def has_role(user: Any, role_slug: str, *, organization: Any = None) -> bool:
    """Return ``True`` if the user holds an active role with the given slug."""
    if not _is_active_user(user):
        return False
    return role_slug in active_role_slugs_for_user(user, organization=organization)


# --- Conceptual object-level checks ---------------------------------------
# These signatures are stable across phases; the bodies grow as the related
# domains are implemented.  Until then they enforce the baseline and remain
# easy to unit-test.


def can_view_checklist(user: Any, checklist: Any) -> bool:
    """Whether ``user`` may view ``checklist``."""
    return _is_active_user(user)


def can_execute_checklist(user: Any, execution: Any) -> bool:
    """Whether ``user`` may execute / continue ``execution``."""
    return _is_active_user(user)


def can_manage_finding(user: Any, finding: Any) -> bool:
    """Whether ``user`` may manage (raise, edit, close) ``finding``."""
    return _is_active_user(user)


def can_verify_action(user: Any, corrective_action: Any) -> bool:
    """Whether ``user`` may verify a corrective action."""
    return _is_active_user(user)
