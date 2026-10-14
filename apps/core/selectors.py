"""Read-only query helpers (selectors) for the core app."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from .models import ActivityLog


def recent_activity(*, organization_id: Any = None, limit: int = 20) -> QuerySet[ActivityLog]:
    """Return recent activity log entries, optionally scoped to an organization."""
    qs = ActivityLog.objects.select_related("actor", "organization")
    if organization_id is not None:
        qs = qs.filter(organization_id=organization_id)
    return qs[:limit]
