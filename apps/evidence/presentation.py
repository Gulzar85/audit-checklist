"""View-model of the evidence panel shown under a checklist question (workspace and read-only detail)."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.template.defaultfilters import filesizeformat
from django.urls import reverse

from .constants import AssetStatus
from .selectors import is_servable
from .validation import IMAGE_TYPES, describe_limits

STATUS_TEXT = {
    AssetStatus.PENDING_SCAN: ("Security check pending", "warning"),
    AssetStatus.AVAILABLE: ("Available and verified", "success"),
    AssetStatus.QUARANTINED: ("Quarantined - not usable", "danger"),
}


def attachment_row(attachment: Any) -> dict[str, Any]:
    asset = attachment.asset
    text, tone = STATUS_TEXT[asset.status]
    servable = is_servable(asset)
    return {
        "id": str(attachment.pk), "name": asset.original_name, "size": filesizeformat(asset.size), "content_type": asset.content_type, "kind": asset.kind,
        "status": asset.status, "status_label": text, "tone": tone, "caption": attachment.caption, "detached": attachment.detached_at is not None,
        "is_image": asset.content_type in IMAGE_TYPES and servable, "can_open": servable,
        "download_url": reverse("evidence:download", args=[attachment.pk]) if servable else "",
        "preview_url": reverse("evidence:preview", args=[attachment.pk]) if servable and asset.content_type in IMAGE_TYPES else "",
        "detach_url": reverse("evidence:detach", args=[attachment.pk]),
    }


def panel_for(execution: Any, spec: Any, result: Any, attachments: list[Any], *, editable: bool) -> dict[str, Any]:
    """``spec`` = the ``ItemSpec``, ``result`` = the engine's ``ItemResult`` for the item."""
    limits = describe_limits()
    config = spec.config if spec.answer_type in ("photo", "file") else {}
    exts = [e for e in (config.get("allowed_extensions") or limits["extensions"]) if e in limits["extensions"]]
    if spec.answer_type == "photo" or (not config and spec.answer_type == "signature"):
        exts = [e for e in exts if e != "pdf"]
    max_files = min(int(config.get("max_files") or limits["max_per_target"]), limits["max_per_target"])
    rows = [attachment_row(a) for a in attachments]
    ready = sum(1 for a in attachments if a.asset.status == AssetStatus.AVAILABLE)
    need = int((result.evidence or {}).get("min_count", 1 if result.required and spec.answer_type in ("photo", "file", "signature") else 0))
    kinds = tuple((result.evidence or {}).get("kinds", ()))
    requirement = ""
    if need:
        requirement = f"At least {need} verified file{'s' if need != 1 else ''} required" + (f" ({' or '.join(kinds)})" if kinds else "") + f" - {ready} verified so far."
    return {
        "item_id": str(spec.id), "execution_id": str(execution.pk), "attachments": rows, "can_upload": editable and len(rows) < max_files, "editable": editable,
        "max_files": max_files, "accept": ",".join(f".{e}" for e in exts) + (",image/jpeg,image/png,image/webp" if any(e != "pdf" for e in exts) else ""),
        "capture": "environment" if spec.answer_type in ("photo", "signature") and config.get("source", "any") != "library" else "",
        "requirement": requirement, "needs_name": spec.answer_type == "signature", "max_mb": limits["max_file_bytes"] // (1024 * 1024),
        "kind": spec.answer_type if spec.answer_type in ("photo", "file", "signature") else "evidence", "extensions": ", ".join(e.upper() for e in exts),
        "any_pending": any(r["status"] == AssetStatus.PENDING_SCAN for r in rows), "scan_note": bool(settings.EVIDENCE_SCAN_REQUIRED),
    }
