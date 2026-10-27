"""Shared plumbing: the audit helper and address masking (no message content, no full addresses and no secrets ever reach the audit log)."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from apps.core.services import log_activity

VERBS = {
    "rule_saved": "notification.rule_saved", "rule_toggled": "notification.rule_toggled", "template_saved": "notification.template_saved",
    "template_activated": "notification.template_activated", "template_archived": "notification.template_archived", "preference_changed": "notification.preference_changed",
    "event_published": "notification.event_published", "generated": "notification.generated", "delivery_attempted": "notification.delivery_attempted",
    "delivery_sent": "notification.delivery_sent", "delivery_retry": "notification.delivery_retry_scheduled", "delivery_failed": "notification.delivery_failed",
    "delivery_exhausted": "notification.delivery_exhausted", "manual_retry": "notification.manual_retry", "resend": "notification.manual_resend",
    "delivery_cancelled": "notification.delivery_cancelled", "delivery_confirmed": "notification.delivery_confirmed", "purged": "notification.retention_purge",
    "event_failed": "notification.event_failed",
}


def audit(verb: str, organization: Any, actor: Any, target: Any, description: str, **metadata: Any) -> None:
    log_activity(verb=verb, organization=organization, actor=actor if getattr(actor, "is_authenticated", False) else None, target=target, description=description[:400], metadata=metadata)


def mask_email(address: str) -> str:
    """``alex@example.com`` -> ``a***@example.com`` (diagnostics without storing the real address)."""
    local, _sep, domain = (address or "").partition("@")
    return f"{local[:1]}***@{domain}"[:120] if domain else ""


def valid_address(address: str) -> bool:
    try:
        validate_email((address or "").strip())
    except ValidationError:
        return False
    return "\n" not in address and "\r" not in address and "," not in address and ";" not in address
