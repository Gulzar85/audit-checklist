"""Email delivery orchestration: claim, send (outside any business transaction), record attempts, bounded retries, manual operations.

States (``constants.TRANSITIONS``, enforced by a trigger): ``pending -> processing -> sent | retry_pending | failed``; ``retry_pending -> processing``; ``sent -> delivered``
(only when a provider confirms - ``confirm_delivery``); ``failed -> pending`` (controlled manual retry).  ``sent`` means *the provider accepted the message*; it is never
reported as delivered.  A delivery stuck in ``retry_pending`` is shown as such - never as success.

* **Claiming** locks a bounded batch with ``SELECT ... FOR UPDATE SKIP LOCKED`` and stamps a lock token, so two dispatchers never send the same delivery; a ``processing`` delivery older
  than the lock timeout (a crashed worker) is claimed again.
* **Sending** happens after the claim transaction committed, one delivery at a time; one provider failure never affects another delivery.
* **Failures** are classified (``EmailProviderError.transient`` / ``category``); only sanitized categories are stored.  Transient failures retry with bounded exponential backoff
  (``BACKOFF_BASE_SECONDS`` x 2^n, capped) until ``max_attempts``; then the delivery is ``failed`` and an operational ``system.delivery_failed`` event is published.
* **Idempotency:** the delivery key is ``<notification>|email|<n>``; a deliberate resend creates the next key with its reason.  Retrying never creates another in-app notification.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .. import constants as k
from .. import policies
from ..models import DeliveryAttempt, NotificationDelivery
from ..providers.base import EmailMessage, EmailProviderError
from . import notification_service
from .base import VERBS, audit, mask_email, valid_address
from .errors import NotificationConflict, NotificationError

logger = logging.getLogger(__name__)


def backoff_seconds(attempt: int) -> int:
    return min(k.BACKOFF_CAP_SECONDS, k.BACKOFF_BASE_SECONDS * (2 ** max(attempt - 1, 0)))


def _dispatchable(now: Any) -> Q:
    stale = now - timedelta(seconds=k.LOCK_TIMEOUT_SECONDS)
    return Q(status=k.DeliveryStatus.PENDING) | Q(status=k.DeliveryStatus.RETRY_PENDING, next_attempt_at__lte=now) | Q(status=k.DeliveryStatus.PROCESSING, locked_at__lt=stale)


def claim(limit: int = 50, *, now: Any = None, event_id: Any = None) -> list[tuple[Any, str]]:
    """Lock up to ``limit`` dispatchable email deliveries for this worker.  Returns ``[(delivery id, lock token)]``."""
    now = now or timezone.now()
    token = uuid.uuid4().hex
    with transaction.atomic():
        qs = NotificationDelivery.objects.select_for_update(skip_locked=True).filter(channel=k.Channel.EMAIL).filter(_dispatchable(now))
        if event_id is not None:
            qs = qs.filter(notification__event_id=event_id)
        ids = list(qs.order_by("created_at").values_list("pk", flat=True)[: max(1, int(limit))])
        if ids:
            NotificationDelivery.objects.filter(pk__in=ids).update(status=k.DeliveryStatus.PROCESSING, locked_at=now, lock_token=token)
    return [(pk, token) for pk in ids]


def _finish(delivery: NotificationDelivery, token: str, *, outcome: str, started: Any, finished: Any, category: str = "", provider: str = "", message_id: str = "",
            status_code: int | None = None, transient: bool = False) -> str:
    """Record the attempt and move the delivery (guarded by the lock token).  Returns the delivery's new status."""
    number = delivery.attempt_count + 1
    duration = max(0, int((finished - started).total_seconds() * 1000))
    with transaction.atomic():
        current = NotificationDelivery.objects.select_for_update().get(pk=delivery.pk)
        if current.status != k.DeliveryStatus.PROCESSING or current.lock_token != token:
            return str(current.status)  # someone else owns it now (a stale worker): record nothing
        DeliveryAttempt.objects.create(delivery=current, number=number, started_at=started, finished_at=finished, outcome=outcome, error_category=category, provider=provider,
                                       provider_message_id=message_id, provider_status=status_code, duration_ms=duration)
        fields: dict[str, Any] = {"attempt_count": number, "last_attempt_at": finished, "locked_at": None, "lock_token": "", "provider": provider or current.provider,
                                  "last_error_category": category, "last_error": str(k.ErrorCategory(category).label)[:200] if category in k.ErrorCategory.values else ""}
        if outcome == k.AttemptOutcome.ACCEPTED:
            fields.update(status=k.DeliveryStatus.SENT, accepted_at=finished, provider_message_id=message_id, next_attempt_at=None, last_error_category="", last_error="")
        elif transient and number < current.max_attempts:
            fields.update(status=k.DeliveryStatus.RETRY_PENDING, next_attempt_at=finished + timedelta(seconds=backoff_seconds(number)))
        else:
            fields.update(status=k.DeliveryStatus.FAILED, next_attempt_at=None)
        NotificationDelivery.objects.filter(pk=current.pk).update(**fields)
        status = str(fields["status"])
    organization, notification = delivery.organization, delivery.notification
    audit(VERBS["delivery_attempted"], organization, None, delivery, f"Email attempt {number} {outcome}", delivery=str(delivery.pk), attempt=number, outcome=outcome, category=category, provider=provider)
    if status == k.DeliveryStatus.SENT:
        audit(VERBS["delivery_sent"], organization, None, delivery, "Email accepted by the provider", delivery=str(delivery.pk), provider=provider)
    elif status == k.DeliveryStatus.RETRY_PENDING:
        audit(VERBS["delivery_retry"], organization, None, delivery, f"Email retry scheduled after attempt {number}", delivery=str(delivery.pk), category=category, attempt=number)
    else:
        exhausted = transient
        audit(VERBS["delivery_exhausted"] if exhausted else VERBS["delivery_failed"], organization, None, delivery,
              "Email failed after the allowed attempts" if exhausted else "Email failed permanently", delivery=str(delivery.pk), category=category, attempts=number)
        _announce_failure(delivery, notification, category)
    return status


def _announce_failure(delivery: NotificationDelivery, notification: Any, category: str) -> None:
    if notification.event_type == "system.delivery_failed":
        return  # never notify about the failure of a failure notice
    try:
        from . import events

        events.publish("system.delivery_failed", delivery, discriminator=str(delivery.pk), metadata={"error_category": category})
    except Exception:  # noqa: BLE001 - the operational notice is best-effort and must not break the dispatcher
        logger.exception("Could not publish the delivery failure notice")


def send_claimed(pk: Any, token: str) -> str:
    """Send one claimed delivery.  Never raises; returns the delivery's resulting status."""
    delivery = NotificationDelivery.objects.select_related("notification__recipient", "organization", "notification").get(pk=pk)
    if delivery.status != k.DeliveryStatus.PROCESSING or delivery.lock_token != token:
        return str(delivery.status)
    started = timezone.now()
    user = delivery.notification.recipient
    address = str(getattr(user, "email", "") or "").strip()
    if not user.is_active:
        return _finish(delivery, token, outcome=k.AttemptOutcome.PERMANENT, started=started, finished=timezone.now(), category=k.ErrorCategory.NO_ADDRESS)
    if not valid_address(address):
        return _finish(delivery, token, outcome=k.AttemptOutcome.PERMANENT, started=started, finished=timezone.now(), category=k.ErrorCategory.NO_ADDRESS)
    message = EmailMessage(subject=f"{getattr(settings, 'EMAIL_SUBJECT_PREFIX', '')}{delivery.subject}"[:300], body=delivery.body_text, to=[address], html_body=delivery.body_html or None)
    provider_name = ""
    try:
        provider = notification_service.get_email_provider()
        provider_name = getattr(provider, "name", "") or type(provider).__name__
        result = provider.send(message)
    except EmailProviderError as exc:
        category = exc.category if exc.category in k.ErrorCategory.values else k.ErrorCategory.UNEXPECTED
        return _finish(delivery, token, outcome=k.AttemptOutcome.TRANSIENT if exc.transient else k.AttemptOutcome.PERMANENT, started=started, finished=timezone.now(), category=category,
                       provider=provider_name, status_code=exc.status_code, transient=exc.transient)
    except Exception:  # noqa: BLE001 - an unexpected provider bug is a transient failure with a sanitized category
        logger.exception("Unexpected error sending delivery %s", delivery.pk)
        return _finish(delivery, token, outcome=k.AttemptOutcome.TRANSIENT, started=started, finished=timezone.now(), category=k.ErrorCategory.UNEXPECTED, provider=provider_name, transient=True)
    return _finish(delivery, token, outcome=k.AttemptOutcome.ACCEPTED, started=started, finished=timezone.now(), provider=getattr(result, "provider", provider_name) or provider_name,
                   message_id=getattr(result, "provider_message_id", "") or "", status_code=getattr(result, "status_code", None))


def dispatch_batch(limit: int | None = None, *, event_id: Any = None, now: Any = None) -> dict[str, int]:
    """Claim and send a bounded batch.  Safe to run concurrently and repeatedly (the command and the Phase 18 scheduler call this)."""
    limit = int(limit or getattr(settings, "NOTIFICATION_BATCH_LIMIT", 50))
    claimed = claim(limit, now=now, event_id=event_id)
    counts = {"claimed": len(claimed), "sent": 0, "retry": 0, "failed": 0}
    for pk, token in claimed:
        try:
            status = send_claimed(pk, token)
        except Exception:  # noqa: BLE001 - one delivery never stops the batch
            logger.exception("Dispatch of %s failed", pk)
            continue
        key = {k.DeliveryStatus.SENT: "sent", k.DeliveryStatus.RETRY_PENDING: "retry", k.DeliveryStatus.FAILED: "failed"}.get(status)
        if key:
            counts[key] += 1
    return counts


def dispatch_for_event(event_pk: Any) -> dict[str, int]:
    return dispatch_batch(event_id=event_pk)


# ---------------------------------------------------------------------------------------------------------------------------------
# Controlled operations
# ---------------------------------------------------------------------------------------------------------------------------------
def _authorize(delivery: NotificationDelivery, actor: Any, reason: str) -> str:
    if not policies.can_retry(actor, delivery.organization):
        raise PermissionDenied
    text = (reason or "").strip()
    if not text:
        raise NotificationError("A reason is required.", "reason_required")
    return text[:300]


@transaction.atomic
def retry(delivery: NotificationDelivery, *, actor: Any, reason: str) -> NotificationDelivery:
    """Controlled manual retry of a ``failed`` email delivery: back to ``pending`` with a fresh bounded attempt allowance."""
    text = _authorize(delivery, actor, reason)
    locked = NotificationDelivery.objects.select_for_update().select_related("organization").get(pk=delivery.pk)
    if locked.channel != k.Channel.EMAIL or locked.status != k.DeliveryStatus.FAILED:
        raise NotificationConflict("Only a failed email delivery can be retried.", "not_failed")
    NotificationDelivery.objects.filter(pk=locked.pk).update(status=k.DeliveryStatus.PENDING, max_attempts=locked.attempt_count + int(getattr(settings, "NOTIFICATION_MAX_ATTEMPTS", k.MAX_ATTEMPTS_DEFAULT)),
                                                             next_attempt_at=None, locked_at=None, lock_token="")
    audit(VERBS["manual_retry"], locked.organization, actor, locked, "Manual retry of a failed email", delivery=str(locked.pk), reason=text)
    locked.refresh_from_db()
    return locked


@transaction.atomic
def resend(delivery: NotificationDelivery, *, actor: Any, reason: str) -> NotificationDelivery:
    """Deliberate resend: a **new** delivery of the same notification (same content), with its own key, the original linked and the reason recorded."""
    text = _authorize(delivery, actor, reason)
    locked = NotificationDelivery.objects.select_for_update().select_related("organization", "notification__recipient").get(pk=delivery.pk)
    if locked.channel != k.Channel.EMAIL or locked.status not in (k.DeliveryStatus.SENT, k.DeliveryStatus.DELIVERED, k.DeliveryStatus.FAILED):
        raise NotificationConflict("Only a sent, delivered or failed email can be sent again.", "not_resendable")
    root = locked.resend_of or locked
    seq = NotificationDelivery.objects.filter(notification_id=locked.notification_id, channel=k.Channel.EMAIL).count()
    new = NotificationDelivery.objects.create(
        organization=locked.organization, notification=locked.notification, channel=k.Channel.EMAIL, idempotency_key=f"{locked.notification_id}|email|{seq}", resend_of=root,
        resend_reason=text, to_masked=mask_email(str(locked.notification.recipient.email or "")), subject=locked.subject, body_text=locked.body_text, body_html=locked.body_html,
        max_attempts=int(getattr(settings, "NOTIFICATION_MAX_ATTEMPTS", k.MAX_ATTEMPTS_DEFAULT)))
    audit(VERBS["resend"], locked.organization, actor, new, "Manual resend of an email", delivery=str(new.pk), original=str(root.pk), reason=text)
    return new


@transaction.atomic
def cancel(delivery: NotificationDelivery, *, actor: Any, reason: str) -> NotificationDelivery:
    text = _authorize(delivery, actor, reason)
    locked = NotificationDelivery.objects.select_for_update().select_related("organization").get(pk=delivery.pk)
    if locked.status not in (k.DeliveryStatus.PENDING, k.DeliveryStatus.RETRY_PENDING, k.DeliveryStatus.FAILED):
        raise NotificationConflict("Only a pending, waiting or failed delivery can be cancelled.", "not_cancellable")
    NotificationDelivery.objects.filter(pk=locked.pk).update(status=k.DeliveryStatus.CANCELLED, next_attempt_at=None, locked_at=None, lock_token="", suppressed_reason="cancelled")
    audit(VERBS["delivery_cancelled"], locked.organization, actor, locked, "Delivery cancelled", delivery=str(locked.pk), reason=text)
    locked.refresh_from_db()
    return locked


def confirm_delivery(delivery: NotificationDelivery, *, provider_message_id: str = "") -> NotificationDelivery:
    """Interface for a provider that *confirms* delivery (a webhook / status poll): ``sent -> delivered``.  SMTP and Graph ``sendMail`` never call this."""
    with transaction.atomic():
        locked = NotificationDelivery.objects.select_for_update().select_related("organization").get(pk=delivery.pk)
        if locked.status == k.DeliveryStatus.DELIVERED:
            return locked
        if locked.status != k.DeliveryStatus.SENT:
            raise NotificationConflict("Only a sent delivery can be confirmed.", "not_sent")
        NotificationDelivery.objects.filter(pk=locked.pk).update(status=k.DeliveryStatus.DELIVERED, delivered_at=timezone.now(),
                                                                 provider_message_id=provider_message_id or locked.provider_message_id)
    audit(VERBS["delivery_confirmed"], locked.organization, None, locked, "Delivery confirmed by the provider", delivery=str(locked.pk))
    locked.refresh_from_db()
    return locked
