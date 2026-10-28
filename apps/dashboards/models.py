"""Dashboard models (Phase 20).  Metrics are never stored: only the *layout* - which registered widgets, in which order, which hidden."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel


class DashboardLayout(BaseModel):
    """A personal layout (``user`` set) or the organization's shared default (``user`` empty, edited with ``dashboard.manage``) for one dashboard.

    ``widgets`` is ``[{"code": "<registered widget>", "hidden": false}, ...]`` - validated against the registry when saved and again when loaded."""

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="dashboard_layouts")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="dashboard_layouts")
    dashboard = models.CharField(max_length=40)
    widgets = models.JSONField(default=list)
    revision = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "user", "dashboard"], condition=models.Q(user__isnull=False), name="dashboard_layout_unique_personal"),
            models.UniqueConstraint(fields=["organization", "dashboard"], condition=models.Q(user__isnull=True), name="dashboard_layout_unique_shared"),
        ]

    def __str__(self) -> str:
        return f"{self.dashboard} ({'shared' if self.user_id is None else self.user_id})"
