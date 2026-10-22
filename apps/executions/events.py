"""Extension points for later phases (offline sync, notifications, findings): signals sent *after commit*.

Phase 9 delivers nothing: there are no receivers.  Each signal is sent with ``sender=ChecklistExecution``, ``execution`` and
``actor``, only once the causing transaction has committed.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.dispatch import Signal

execution_started = Signal()
execution_submitted = Signal()
execution_cancelled = Signal()


def send_on_commit(signal: Signal, execution: Any, actor: Any = None, **extra: Any) -> None:
    transaction.on_commit(lambda: signal.send(sender=type(execution), execution=execution, actor=actor, **extra))
