"""Integration points for the future Workflow Engine (Phase 14) and notifications: signals sent *after commit*.

Phase 13 delivers nothing: there are no receivers.  Each signal is sent with ``sender=CorrectiveAction``, ``action`` and ``actor``
plus extras, only once the causing transaction has committed, so a receiver never sees a rolled-back change.  Receivers must call the
lifecycle services - they must never write ``status``.  No outbox table is used: the signals are in-process and the audit log is the
durable record; a later phase that needs guaranteed delivery adds an outbox then.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.dispatch import Signal

action_created = Signal()
action_issued = Signal()
action_assigned = Signal()  # extras: changes
action_updated = Signal()  # extras: changes
action_progress_added = Signal()  # extras: update
action_started = Signal()
action_blocked = Signal()
action_resumed = Signal()
action_submitted = Signal()
action_rejected = Signal()
action_verified = Signal()
action_closed = Signal()
action_cancelled = Signal()
action_reopened = Signal()
#: Sent by ``selectors``-driven jobs of a later phase (overdue is derived; nothing sends this in Phase 13).
action_became_overdue = Signal()

BY_TRANSITION = {
    "issue": action_issued, "start": action_started, "block": action_blocked, "resume": action_resumed, "submit": action_submitted,
    "verify": action_verified, "reject": action_rejected, "close": action_closed, "cancel": action_cancelled, "reopen": action_reopened,
}


def send_on_commit(signal: Signal, action: Any, actor: Any = None, **extra: Any) -> None:
    transaction.on_commit(lambda: signal.send(sender=type(action), action=action, actor=actor, **extra))
