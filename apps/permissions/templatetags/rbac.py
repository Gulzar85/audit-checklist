"""Template helpers for *showing* controls the user may use.

``{% can "location.update" location as ok %}`` is a display hint only: hiding a button is not
authorization.  Every destination re-checks the same permission on the server.
"""

from __future__ import annotations

from typing import Any

from django import template

from apps.permissions import registry
from apps.permissions.authorization import has_permission, has_permission_anywhere

register = template.Library()


@register.simple_tag(takes_context=True)
def can(context: Any, code: str, obj: Any = None) -> bool:
    """Whether the current user holds ``code`` at ``obj`` (a Location, an Organization, or nothing)."""
    user = getattr(context.get("request"), "user", None) or context.get("user")
    if not registry.is_registered(code):  # a typo in a template must be loud, not a silent hide
        raise template.TemplateSyntaxError(f"Unknown permission {code!r}")
    if obj is None:
        return has_permission_anywhere(user, code)
    if hasattr(obj, "parent_id"):  # a Location
        return has_permission(user, code, location=obj)
    return has_permission(user, code, organization=getattr(obj, "organization_id", None) or obj)
