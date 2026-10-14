"""Reusable abstract models and the business activity log.

Model history (django-simple-history) and the business activity log are two
different things:

* **Model history** records field-level changes to a single model instance.
* **ActivityLog** records meaningful business events across the platform
  (e.g. "checklist published", "finding raised", "action verified").
"""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.db import models


class UUIDModel(models.Model):
    """Adds a UUID primary key, appropriate for externally exposed identifiers."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    """Adds creation/update timestamps."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)

    class Meta:
        abstract = True


class BaseModel(UUIDModel, TimeStampedModel):
    """The standard concrete base for domain models in this platform."""

    class Meta:
        abstract = True


class ActivityLog(UUIDModel):
    """An append-only business activity log."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity_log",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity_log",
    )
    verb = models.CharField(max_length=64, db_index=True)
    description = models.CharField(max_length=512, blank=True)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, editable=False)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "activity log entry"
        verbose_name_plural = "activity log"
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
            models.Index(fields=["target_type", "target_id"]),
            models.Index(fields=["verb", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.verb} @ {self.created_at:%Y-%m-%d %H:%M}" if self.created_at else self.verb

    @property
    def metadata_dict(self) -> dict[str, Any]:
        return self.metadata or {}
