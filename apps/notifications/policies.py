"""Authorization for notifications (ADR 0079-0081).  Every decision calls the central permission service.

* **Your own notifications** need nothing but authentication - and only ever yours: the selectors filter by recipient, so another person's notification is a 404, never a 403.
* **Rules and templates** are organization-level (``notification.view`` / ``notification.configure``); making a rule *mandatory* needs the separate ``notification.mandatory``.
* **Delivery history and operations** (``notification.deliveries``, ``notification.retry``) are organization-wide: deliveries contain recipient and operational information.
* Receiving a notification never authorizes opening its destination: the destination re-checks the viewer's own permission.
"""

from __future__ import annotations

from typing import Any

from apps.core.policies import is_active_user
from apps.permissions.authorization import has_permission, has_permission_anywhere
from apps.permissions.registry import P


def _org(user: Any, code: str, organization: Any) -> bool:
    return is_active_user(user) and organization is not None and has_permission(user, code, organization=getattr(organization, "pk", organization))


def can_view_config(user: Any, organization: Any) -> bool:
    return _org(user, P.NOTIFICATION_VIEW, organization)


def can_configure(user: Any, organization: Any) -> bool:
    return _org(user, P.NOTIFICATION_CONFIGURE, organization)


def can_set_mandatory(user: Any, organization: Any) -> bool:
    return _org(user, P.NOTIFICATION_MANDATORY, organization)


def can_view_deliveries(user: Any, organization: Any) -> bool:
    return _org(user, P.NOTIFICATION_DELIVERIES, organization)


def can_retry(user: Any, organization: Any) -> bool:
    return _org(user, P.NOTIFICATION_RETRY, organization)


def can_manage_anywhere(user: Any) -> bool:
    return is_active_user(user) and (has_permission_anywhere(user, P.NOTIFICATION_VIEW) or has_permission_anywhere(user, P.NOTIFICATION_DELIVERIES))
