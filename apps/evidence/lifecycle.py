"""Evidence lifecycle (ADR 0061): the only place an asset's status changes.

    pending_scan --clean / not required--> available
    pending_scan --infected--------------> quarantined
    available    --late detection-------> quarantined
    (quarantined is final: a quarantined file is never released automatically - the platform has no "mark clean" operation)
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.core.services import log_activity, stamp_history

from .constants import AssetStatus, ScanStatus
from .models import EvidenceAsset
from .scanning import ScanResult, get_scanner, scan_required
from .storage import StorageError, get_storage

TRANSITIONS = {
    AssetStatus.PENDING_SCAN: frozenset({AssetStatus.AVAILABLE, AssetStatus.QUARANTINED}),
    AssetStatus.AVAILABLE: frozenset({AssetStatus.QUARANTINED}),
    AssetStatus.QUARANTINED: frozenset(),
}


class InvalidTransition(Exception):
    pass


def assert_transition(old: str, new: str) -> None:
    if new not in TRANSITIONS.get(old, frozenset()):
        raise InvalidTransition(f"An evidence file cannot go from {old} to {new}.")


def _log(verb: str, asset: EvidenceAsset, description: str, actor: Any = None, **metadata: Any) -> None:
    log_activity(verb=verb, organization=asset.organization, actor=actor, target=asset, description=description,
                 metadata={"asset_id": str(asset.pk), **metadata})


@transaction.atomic
def apply_scan(asset: EvidenceAsset, result: ScanResult) -> EvidenceAsset:
    """Record a scan outcome and move the asset accordingly.  Idempotent for an already-decided asset."""
    locked = EvidenceAsset.objects.select_for_update().select_related("organization").get(pk=asset.pk)
    if locked.status == AssetStatus.QUARANTINED:
        return locked  # final
    now = timezone.now()
    if result.status == "infected":
        assert_transition(locked.status, AssetStatus.QUARANTINED)
        locked.status, locked.scan_status = AssetStatus.QUARANTINED, ScanStatus.INFECTED
        locked.scan_detail, locked.scanned_at = result.detail[:200], now
        stamp_history(locked, None, "evidence.quarantined")
        locked.save()
        _log("evidence.quarantined", locked, f"Quarantined {locked.original_name}", detail=result.detail[:100])
        return locked
    if locked.status == AssetStatus.AVAILABLE:
        return locked
    if result.status == "clean":
        status, scan = AssetStatus.AVAILABLE, ScanStatus.CLEAN
    elif not scan_required():  # explicit development policy: not "clean" - recorded as not required
        status, scan = AssetStatus.AVAILABLE, ScanStatus.NOT_REQUIRED
    else:  # scanner error / unavailable: stays pending, retryable, never available
        locked.scan_status, locked.scan_detail, locked.scanned_at = ScanStatus.ERROR, result.detail[:200], now
        locked.save(update_fields=["scan_status", "scan_detail", "scanned_at", "updated_at"])
        _log("evidence.scan_failed", locked, f"Security check of {locked.original_name} did not complete", detail=result.detail[:100])
        return locked
    assert_transition(locked.status, status)
    locked.status, locked.scan_status, locked.scan_detail, locked.scanned_at = status, scan, result.detail[:200], now
    stamp_history(locked, None, "evidence.available")
    locked.save()
    _log("evidence.available", locked, f"{locked.original_name} is available", scan=scan)
    return locked


def scan_asset(asset: EvidenceAsset) -> EvidenceAsset:
    """Run the configured scanner over the stored bytes (bounded: the scanner has a timeout) and apply the result."""
    try:
        with get_storage().open(asset.storage_key) as handle:
            data = handle.read()
    except StorageError:
        return apply_scan(asset, ScanResult("error", "The stored file could not be read."))
    try:
        result = get_scanner().scan(data)
    except Exception:  # noqa: BLE001 - a broken scanner must never make a file available
        result = ScanResult("error", "The scanner failed.")
    return apply_scan(asset, result)
