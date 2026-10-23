"""Vocabulary of the evidence subsystem (ADR 0060)."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class AssetStatus(models.TextChoices):
    """Lifecycle of a stored file.  ``pending_scan -> available | quarantined``; ``available -> quarantined`` (a late detection)."""

    PENDING_SCAN = "pending_scan", _("Waiting for the security check")
    AVAILABLE = "available", _("Available")
    QUARANTINED = "quarantined", _("Quarantined")


class ScanStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    CLEAN = "clean", _("Clean")
    INFECTED = "infected", _("Infected")
    ERROR = "error", _("Scan failed")
    NOT_REQUIRED = "not_required", _("Not required (development policy)")


class Kind(models.TextChoices):
    """The evidence kinds the rules engine and the answer types speak (``REQUIRE_EVIDENCE.kinds``)."""

    PHOTO = "photo", _("Photo")
    FILE = "file", _("Document")
    SIGNATURE = "signature", _("Signature")


class TargetType(models.TextChoices):
    """Approved attachment targets.  A new type is added deliberately: here, in ``targets.py`` and by a migration (see docs)."""

    CHECKLIST_RESPONSE = "checklist_response", _("Checklist response")
    CORRECTIVE_ACTION = "corrective_action", _("Corrective action")


#: Scan statuses under which an asset may be AVAILABLE.
SAFE_SCAN_STATUSES = (ScanStatus.CLEAN, ScanStatus.NOT_REQUIRED)
