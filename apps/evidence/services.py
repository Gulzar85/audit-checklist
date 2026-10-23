"""Evidence upload / attach / detach / serve - authorization, validation, storage, transactions and audit, in one place.

Failure model: the file is written to private storage *first* (under a generated key), then the database rows are created in one
transaction under the target execution's row lock.  If the transaction fails the file is removed (compensation); if the process dies
in between, ``reconcile_evidence`` removes the orphan.  A file is never reachable by URL, so an orphan is never public.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.services import log_activity, stamp_history

from . import lifecycle, selectors
from .constants import AssetStatus, ScanStatus
from .models import EvidenceAsset, EvidenceAttachment
from .storage import StorageError, get_storage, new_key
from .targets import TargetLocked, TargetNotFound, get_spec
from .validation import Rejected, inspect_upload
from django.conf import settings


class EvidenceError(ValidationError):
    """A refusal with a stable ``evidence_code`` and a user-safe message."""

    def __init__(self, message: str, evidence_code: str, status: int = 400) -> None:
        super().__init__(message, code=evidence_code)
        self.evidence_code, self.http_status = evidence_code, status


@dataclass
class UploadResult:
    asset: EvidenceAsset
    attachment: EvidenceAttachment
    duplicate: bool = False


def _audit(verb: str, organization: Any, actor: Any, target: Any, description: str, **metadata: Any) -> None:
    log_activity(verb=verb, organization=organization, actor=actor, target=target, description=description, metadata=metadata)


def upload(actor: Any, *, target_type: str, ref: dict[str, Any], filename: str, data: bytes, caption: str = "", operation_id: Any = None,
           captured_at: Any = None, client_sha256: str = "") -> UploadResult:
    """Validate, store and attach one file to a registered target; idempotent per ``(actor, operation_id)``."""
    try:
        spec = get_spec(target_type)
        ctx = spec.resolve(actor, ref, create=True)
    except TargetNotFound:
        raise EvidenceError("That item is not available for evidence.", "target_not_found", 404) from None
    try:
        spec.check_can_attach(actor, ctx)
    except TargetLocked as exc:
        raise EvidenceError(exc.message, exc.code, 409) from None
    if client_sha256 and hashlib.sha256(data).hexdigest() != str(client_sha256).lower():
        # The bytes that arrived are not the bytes the device meant to send (truncated / corrupted transfer): nothing is acknowledged.
        raise EvidenceError("The file did not arrive intact. Please try again.", "checksum_mismatch")
    try:
        inspected = inspect_upload(filename, data, kind_hint=ctx.kind_hint, allowed_extensions=ctx.allowed_extensions)
    except Rejected as exc:
        _audit("evidence.rejected", ctx.organization, actor, ctx.target_obj, f"Rejected an upload for {ctx.reference}", code=exc.code)
        raise EvidenceError(exc.message, exc.code) from None
    if operation_id:
        prior = EvidenceAsset.objects.filter(uploaded_by=actor, client_operation_id=operation_id).first()
        if prior is not None:
            if prior.sha256 != inspected.sha256 or prior.organization_id != ctx.organization.pk:
                raise EvidenceError("This operation id was already used for a different file.", "operation_id_reused", 409)
            attachment = (EvidenceAttachment.objects.filter(asset=prior, **ctx.key).order_by("detached_at", "-created_at").first())
            if attachment is not None:
                return UploadResult(prior, attachment, duplicate=True)
    key = new_key()
    storage = get_storage()
    try:
        storage.put(key, inspected.data)
    except StorageError:
        raise EvidenceError("The file could not be stored. Please try again.", "storage_failed", 503) from None
    try:
        with transaction.atomic():
            spec.lock(ctx)  # serialise with saves / submission / state changes of the target
            spec.check_can_attach(actor, ctx)  # re-check under the lock: a submit may have won the race
            live = EvidenceAttachment.objects.filter(**ctx.key, detached_at__isnull=True).count()
            limit = min(int(ctx.max_files or settings.EVIDENCE_MAX_PER_TARGET), int(settings.EVIDENCE_MAX_PER_TARGET))
            if live >= limit:
                raise EvidenceError(f"At most {limit} files can be attached to one question.", "too_many_files")
            asset = EvidenceAsset(
                organization=ctx.organization, uploaded_by=actor, client_operation_id=operation_id or None, original_name=inspected.name, kind=inspected.kind,
                content_type=inspected.content_type, size=len(inspected.data), sha256=inspected.sha256, storage_key=key, width=inspected.width,
                height=inspected.height, captured_at=captured_at,
            )
            stamp_history(asset, actor, "evidence.uploaded")
            asset.save()
            attachment = EvidenceAttachment(asset=asset, organization=ctx.organization, target_type=ctx.target_type, **ctx.key, purpose=ctx.purpose,
                                            caption=(caption or "")[:200], position=live, attached_by=actor)
            stamp_history(attachment, actor, "evidence.attached")
            attachment.save()
            spec.touch(ctx, activity=True)
            _audit("evidence.uploaded", ctx.organization, actor, asset, f"Uploaded {asset.original_name} to {ctx.reference}",
                   asset_id=str(asset.pk), kind=asset.kind, size=asset.size, purpose=ctx.purpose or None, **spec.audit_meta(ctx))
    except IntegrityError as exc:  # a concurrent retry of the same operation id won: replay it
        storage.delete(key)
        winner = EvidenceAsset.objects.filter(uploaded_by=actor, client_operation_id=operation_id).first() if operation_id else None
        if winner is not None and winner.sha256 == inspected.sha256:
            att = EvidenceAttachment.objects.filter(asset=winner, **ctx.key).first()
            if att is not None:
                return UploadResult(winner, att, duplicate=True)
        raise EvidenceError("The file could not be saved. Please try again.", "conflict", 409) from exc
    except Exception:
        storage.delete(key)  # compensation: nothing is left behind
        raise
    asset = lifecycle.scan_asset(asset)
    attachment.refresh_from_db()
    return UploadResult(asset, attachment)


def attach_existing(actor: Any, asset: EvidenceAsset, *, target_type: str, ref: dict[str, Any], caption: str = "") -> EvidenceAttachment:
    """Attach an already-stored, available asset to another registered target (no copy of the file)."""
    if selectors.get_asset(actor, asset.pk) is None:
        raise EvidenceError("That file is not available.", "asset_not_found", 404)
    if asset.status != AssetStatus.AVAILABLE:
        raise EvidenceError("That file is not available yet.", "asset_not_available", 409)
    try:
        spec = get_spec(target_type)
        ctx = spec.resolve(actor, ref, create=False)
    except TargetNotFound:
        raise EvidenceError("That item is not available for evidence.", "target_not_found", 404) from None
    if ctx.organization.pk != asset.organization_id:
        raise EvidenceError("That file is not available.", "asset_not_found", 404)
    try:
        spec.check_can_attach(actor, ctx)
    except TargetLocked as exc:
        raise EvidenceError(exc.message, exc.code, 409) from None
    with transaction.atomic():
        spec.lock(ctx)
        spec.check_can_attach(actor, ctx)
        if EvidenceAttachment.objects.filter(asset=asset, **ctx.key, detached_at__isnull=True).exists():
            raise EvidenceError("That file is already attached here.", "already_attached", 409)
        live = EvidenceAttachment.objects.filter(**ctx.key, detached_at__isnull=True).count()
        if live >= min(int(ctx.max_files or settings.EVIDENCE_MAX_PER_TARGET), int(settings.EVIDENCE_MAX_PER_TARGET)):
            raise EvidenceError("Too many files are attached to this question.", "too_many_files")
        attachment = EvidenceAttachment(asset=asset, organization=ctx.organization, target_type=ctx.target_type, **ctx.key, purpose=ctx.purpose,
                                        caption=(caption or "")[:200], position=live, attached_by=actor)
        stamp_history(attachment, actor, "evidence.attached")
        attachment.save()
        spec.touch(ctx)
        _audit("evidence.attached", ctx.organization, actor, asset, f"Attached {asset.original_name} to {ctx.reference}",
               asset_id=str(asset.pk), purpose=ctx.purpose or None, **spec.audit_meta(ctx))
    return attachment


def detach(actor: Any, attachment: EvidenceAttachment, *, reason: str = "") -> EvidenceAttachment:
    """Detach (never delete): the asset and the history stay; the file no longer counts as the answer's evidence."""
    spec = get_spec(attachment.target_type)
    ctx = spec.context_for(attachment)
    if not spec.can_view(actor, ctx):
        raise EvidenceError("That attachment was not found.", "attachment_not_found", 404)
    with transaction.atomic():
        spec.lock(ctx)
        try:
            spec.check_can_attach(actor, ctx)
        except TargetLocked as exc:
            raise EvidenceError(exc.message, exc.code, 409) from None
        except PermissionDenied:
            raise
        row = EvidenceAttachment.objects.select_for_update(of=("self",)).get(pk=attachment.pk)
        if row.detached_at is not None:
            return row  # idempotent
        row.detached_at, row.detached_by, row.detach_reason = timezone.now(), actor, (reason or "")[:200]
        stamp_history(row, actor, "evidence.detached")
        row.save()
        spec.touch(ctx)
        _audit("evidence.detached", row.organization, actor, row.asset, f"Detached {row.asset.original_name} from {ctx.reference}",
               asset_id=str(row.asset_id), reason=row.detach_reason, **spec.audit_meta(ctx))
    return row


def record_access(actor: Any, asset: EvidenceAsset, how: str) -> None:
    _audit("evidence.downloaded" if how == "download" else "evidence.previewed", asset.organization, actor, asset, f"{how.title()} of {asset.original_name}",
           asset_id=str(asset.pk))


def read_bytes(asset: EvidenceAsset) -> bytes:
    try:
        with get_storage().open(asset.storage_key) as handle:
            return handle.read()
    except StorageError:
        raise EvidenceError("The file is not available.", "file_missing", 404) from None


__all__ = ["EvidenceError", "ScanStatus", "UploadResult", "attach_existing", "detach", "read_bytes", "record_access", "upload"]
