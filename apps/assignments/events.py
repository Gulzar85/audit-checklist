"""Extension points for later phases (notifications, scheduling, execution): Django signals, sent *after commit*.

Phase 8 delivers nothing: there are no receivers.  A future notification module connects to these signals; it never changes
assignment code.  Each is sent with ``sender=ChecklistAssignment``, ``assignment`` and ``actor`` (plus ``changes`` where
relevant), only once the transaction that caused it has committed, so a receiver never sees a rolled-back change.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.dispatch import Signal

assignment_issued = Signal()
assignment_cancelled = Signal()
assignment_reopened = Signal()
assignment_assignee_changed = Signal()
assignment_due_changed = Signal()
assignment_priority_changed = Signal()


def send_on_commit(signal: Signal, assignment: Any, actor: Any = None, **extra: Any) -> None:
    transaction.on_commit(lambda: signal.send(sender=type(assignment), assignment=assignment, actor=actor, **extra))
