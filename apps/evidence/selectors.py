"""Read-only evidence queries.  Visibility is *derived from the target*: you can see evidence if you can see what it is attached to."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.conf import settings
from django.db.models import QuerySet

from apps.core.utils import parse_uuid

from .constants import AssetStatus
from .models import EvidenceAsset, EvidenceAttachment
from .targets import get_spec


def attachments_for_execution(execution: Any, *, include_detached: bool = False) -> QuerySet[EvidenceAttachment]:
    qs = EvidenceAttachment.objects.filter(execution=execution).select_related("asset", "item")
    return qs if include_detached else qs.filter(detached_at__isnull=True)


def by_item(execution: Any) -> dict[str, list[EvidenceAttachment]]:
    """``{str(item id): [live attachments]}`` in one query."""
    grouped: dict[str, list[EvidenceAttachment]] = defaultdict(list)
    for attachment in attachments_for_execution(execution):
        grouped[str(attachment.item_id)].append(attachment)
    return grouped


def is_servable(asset: EvidenceAsset) -> bool:
    """May the bytes be served at all?  Quarantined never; pending only if the policy says so."""
    if asset.status == AssetStatus.AVAILABLE:
        return True
    return bool(asset.status == AssetStatus.PENDING_SCAN and settings.EVIDENCE_ALLOW_VIEW_PENDING)


def _can_view(user: Any, attachment: EvidenceAttachment) -> bool:
    spec = get_spec(attachment.target_type)
    return spec.can_view(user, spec.context_for(attachment))


def get_attachment(user: Any, pk: Any) -> EvidenceAttachment | None:
    """The attachment if the user may see its target (detached ones stay visible as history), else ``None`` (404)."""
    parsed = parse_uuid(pk)
    if parsed is None:
        return None
    attachment = (EvidenceAttachment.objects.filter(pk=parsed)
                  .select_related("asset", "item", "organization", "execution__assignment__location", "execution__organization").first())
    return attachment if attachment is not None and _can_view(user, attachment) else None


def get_asset(user: Any, pk: Any) -> EvidenceAsset | None:
    """The asset if the user may see *any* attachment of it (that is the only way to reach a file), else ``None`` (404)."""
    parsed = parse_uuid(pk)
    if parsed is None:
        return None
    asset = EvidenceAsset.objects.filter(pk=parsed).select_related("organization").first()
    if asset is None:
        return None
    for attachment in (EvidenceAttachment.objects.filter(asset=asset).select_related("item", "organization", "execution__assignment__location", "execution__organization")[:20]):
        if _can_view(user, attachment):
            return asset
    return None


def attachments_visible_to(user: Any, *, execution: Any = None, item: Any = None) -> list[EvidenceAttachment]:
    """Live attachments of one execution (optionally one item) the user may see."""
    from apps.executions import selectors as exec_selectors

    if execution is None:
        return []
    visible = exec_selectors.get_execution(user, execution.pk if hasattr(execution, "pk") else execution)
    if visible is None:
        return []
    qs = attachments_for_execution(visible)
    if item is not None:
        qs = qs.filter(item=item)
    return list(qs)
