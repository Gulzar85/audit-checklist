"""Read-only query helpers (selectors) for the core app."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from .models import ActivityLog


def recent_activity(
    *,
    organization_id: Any = None,
    target: Any = None,
    limit: int = 20,
) -> QuerySet[ActivityLog]:
    """Return recent activity log entries.

    ``organization_id`` narrows to one organization; ``target`` (any model
    instance) narrows to the events recorded against that object.
    """
    qs = ActivityLog.objects.select_related("actor", "organization")
    if organization_id is not None:
        qs = qs.filter(organization_id=organization_id)
    if target is not None:
        qs = qs.filter(target_type=target._meta.label_lower, target_id=str(target.pk))
    return qs[:limit]
