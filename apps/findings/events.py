"""Extension points for later phases (corrective actions, notifications, escalation): signals sent *after commit*.

Phase 12 delivers nothing: there are no receivers.  Each signal is sent with ``sender=Finding``, ``finding`` and ``actor`` (plus
extras), only once the causing transaction has committed, so a receiver never sees a rolled-back change.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.dispatch import Signal

finding_raised = Signal()
finding_status_changed = Signal()  # extras: action, previous_status
finding_assigned = Signal()
finding_updated = Signal()  # extras: changes


def send_on_commit(signal: Signal, finding: Any, actor: Any = None, **extra: Any) -> None:
    transaction.on_commit(lambda: signal.send(sender=type(finding), finding=finding, actor=actor, **extra))
