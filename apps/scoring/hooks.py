"""Controlled integration points of the scoring engine - registered by other apps (Phase 16 evaluation), never edited here.

``after_recalculate``: callables ``hook(execution, actor, new_result)`` run **inside the recalculation transaction**, after the new current ``ScoreResult``
was written.  A hook must not raise into the recalculation and must be idempotent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

after_recalculate: list[Callable[[Any, Any, Any], Any]] = []


def register_after_recalculate(hook: Callable[[Any, Any, Any], Any]) -> None:
    if hook not in after_recalculate:
        after_recalculate.append(hook)
