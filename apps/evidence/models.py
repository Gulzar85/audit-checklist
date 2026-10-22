"""The evidence data model: a stored file (``EvidenceAsset``) and its link to an approved business record (``EvidenceAttachment``).

Design (ADR 0060):
* the **file** is separate from what it is *about*: one asset can be attached to several records without copying it;
* attachment targets are **explicit foreign keys**, not a client-controlled generic relation: a checklist response is the pair
  (execution, item) - which keeps referential integrity and the exact version - and every future target adds its own nullable key
  through the registry in ``targets.py``;
* a database trigger (``guards.py``) keeps organizations consistent and the file facts (hash, size, key, type) immutable;
* nothing is ever hard-deleted: an attachment is *detached* (who, when, why) and the asset is kept for the audit trail.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel

from .constants import SAFE_SCAN_STATUSES, AssetStatus, Kind, ScanStatus, TargetType

User = settings.AUTH_USER_MODEL


class EvidenceAsset(BaseModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="evidence_assets")
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    #: Idempotency key of an offline / retried upload (unique per uploader); ``None`` for ordinary uploads.
    client_operation_id = models.UUIDField(null=True, blank=True)
    #: The *display* name: normalized, never used as a path.
    original_name = models.CharField(max_length=255)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    #: Decided by the server from the file's bytes, never from the client.
    content_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField()
    #: SHA-256 of the stored bytes (after re-encoding), computed by the server.
    sha256 = models.CharField(max_length=64)
    #: Generated, unpredictable key inside the private storage (not a path the user can influence).
    storage_key = models.CharField(max_length=80, unique=True)
    status = models.CharField(max_length=12, choices=AssetStatus.choices, default=AssetStatus.PENDING_SCAN, db_index=True)
    scan_status = models.CharField(max_length=12, choices=ScanStatus.choices, default=ScanStatus.PENDING)
    scan_detail = models.CharField(max_length=200, blank=True)
    scanned_at = models.DateTimeField(null=True, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    #: The device's claim of when it was captured - diagnostic only, never authoritative.
    captured_at = models.DateTimeField(null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["uploaded_by", "client_operation_id"], condition=models.Q(client_operation_id__isnull=False),
                                    name="one_asset_per_user_operation"),
            models.CheckConstraint(condition=models.Q(status__in=[s for s, _l in AssetStatus.choices]), name="evidence_status_valid"),
            models.CheckConstraint(condition=models.Q(scan_status__in=[s for s, _l in ScanStatus.choices]), name="evidence_scan_status_valid"),
            models.CheckConstraint(condition=models.Q(kind__in=[k for k, _l in Kind.choices]), name="evidence_kind_valid"),
            # An asset can only be AVAILABLE if a scan said clean (or the policy explicitly did not require one) - never "by default".
            models.CheckConstraint(
                condition=~models.Q(status=AssetStatus.AVAILABLE) | models.Q(scan_status__in=[s.value for s in SAFE_SCAN_STATUSES]),
                name="evidence_available_requires_safe_scan",
            ),
            models.CheckConstraint(condition=models.Q(size__gt=0) & models.Q(sha256__regex=r"^[0-9a-f]{64}$"), name="evidence_size_and_hash_valid"),
        ]
        indexes = [models.Index(fields=["organization", "status"]), models.Index(fields=["sha256"]), models.Index(fields=["uploaded_by", "created_at"])]

    def __str__(self) -> str:
        return f"{self.original_name} ({self.get_status_display()})"

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")

    @property
    def is_available(self) -> bool:
        return self.status == AssetStatus.AVAILABLE


class EvidenceAttachment(BaseModel):
    """An asset attached to an approved target (today: one checklist item within one execution)."""

    asset = models.ForeignKey(EvidenceAsset, on_delete=models.PROTECT, related_name="attachments")
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="evidence_attachments")
    target_type = models.CharField(max_length=24, choices=TargetType.choices)
    # -- target: checklist response = (execution, item) ---------------------------------------------------------------------
    execution = models.ForeignKey("executions.ChecklistExecution", on_delete=models.PROTECT, null=True, blank=True, related_name="evidence_attachments")
    item = models.ForeignKey("checklists.ChecklistItem", on_delete=models.PROTECT, null=True, blank=True, related_name="evidence_attachments")
    caption = models.CharField(max_length=200, blank=True)
    position = models.PositiveIntegerField(default=0)
    attached_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    detached_at = models.DateTimeField(null=True, blank=True)
    detached_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    detach_reason = models.CharField(max_length=200, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ("position", "created_at")
        constraints = [
            # An asset is attached to a given target once at a time (re-attaching after detaching is allowed).
            models.UniqueConstraint(fields=["asset", "execution", "item"], condition=models.Q(detached_at__isnull=True), name="one_live_attachment_per_target"),
            models.CheckConstraint(condition=models.Q(target_type__in=[t for t, _l in TargetType.choices]), name="attachment_target_type_valid"),
            models.CheckConstraint(
                condition=~models.Q(target_type=TargetType.CHECKLIST_RESPONSE) | (models.Q(execution__isnull=False) & models.Q(item__isnull=False)),
                name="attachment_response_target_complete",
            ),
            models.CheckConstraint(condition=models.Q(detached_at__isnull=True, detach_reason="") | models.Q(detached_at__isnull=False), name="attachment_detach_consistent"),
        ]
        indexes = [models.Index(fields=["execution", "item", "detached_at"]), models.Index(fields=["organization", "target_type"]), models.Index(fields=["asset"])]

    def __str__(self) -> str:
        return f"{self.asset_id} -> {self.target_type}"

    @property
    def is_detached(self) -> bool:
        return self.detached_at is not None
