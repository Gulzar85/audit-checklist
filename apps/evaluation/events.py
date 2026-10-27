"""Evaluation events - signals sent *after commit* (Phase 17 notifications subscribe; any later module may).

``evaluation_recorded`` (extra: ``run``): a new current evaluation run was recorded for an execution (never for an unchanged re-evaluation).  Sent with
``sender=EvaluationRun``, ``run`` and ``actor`` only once the causing transaction has committed.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.dispatch import Signal

evaluation_recorded = Signal()


def send_on_commit(run: Any, actor: Any = None) -> None:
    transaction.on_commit(lambda: evaluation_recorded.send(sender=type(run), run=run, actor=actor))
