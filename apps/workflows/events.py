"""Workflow events - signals sent *after commit* (Phase 17 notifications subscribe; any later module may).

``task_created`` (extra: ``task``): a task needs someone's decision.  ``instance_finished``: a workflow completed (approved / rejected through its end state) or was cancelled.
Each is sent with ``sender=WorkflowInstance``, ``instance`` and ``actor`` only once the causing transaction has committed, so a receiver never sees a rolled-back change.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.dispatch import Signal

task_created = Signal()
instance_finished = Signal()


def send_on_commit(signal: Signal, instance: Any, actor: Any = None, **extra: Any) -> None:
    transaction.on_commit(lambda: signal.send(sender=type(instance), instance=instance, actor=actor, **extra))
