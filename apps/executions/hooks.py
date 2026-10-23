"""Controlled integration points of the execution lifecycle - registered by other apps (Phase 15 scoring), never edited here.

``after_submit``: callables ``hook(execution, actor, state)`` run **inside the submission transaction**, after the execution became
submitted and findings were detected.  A hook must not raise into the submission (the caller guards it with a savepoint anyway) and must
be idempotent, because a retried submission may call it again.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

after_submit: list[Callable[[Any, Any, Any], Any]] = []


def register_after_submit(hook: Callable[[Any, Any, Any], Any]) -> None:
    if hook not in after_submit:
        after_submit.append(hook)
