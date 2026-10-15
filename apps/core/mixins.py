"""Reusable class-based-view mixins.

Only mixins that solve a repeated problem live here.  The one concern they
share is *where authorization decisions come from*: views never inspect roles
or permissions themselves, they ask a policy (``apps/*/policies.py``).

Status-code semantics used across the web UI and the API:

* an object outside the user's visible scope is **404** (its existence is not
  revealed) - achieved by scoping ``get_queryset`` through a selector;
* an object the user can see but may not act on is **403**.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied


class PolicyMixin(LoginRequiredMixin):
    """Gate a view behind a policy decision.

    Subclasses implement :meth:`policy_allows`.  For views that act on an
    object (detail/update/status) the check runs after the object has been
    fetched from the *scoped* queryset, so out-of-scope objects 404 first.  For
    collection/create views (no object) it runs in ``dispatch`` with
    ``obj=None``.
    """

    #: Set to ``True`` on views that load an object via ``get_object``.
    policy_uses_object = False

    def policy_allows(self, obj: Any = None) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def deny(self) -> None:
        raise PermissionDenied

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated and not self.policy_uses_object:
            if not self.policy_allows(None):
                self.deny()
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, queryset=None):  # noqa: ANN001, ANN201
        obj = super().get_object(queryset)
        if self.policy_uses_object and not self.policy_allows(obj):
            self.deny()
        return obj
