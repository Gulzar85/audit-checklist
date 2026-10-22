"""Hooks other apps register to take part in an assignment's lifecycle *inside its transaction*.

Assignments never import the execution engine (Phase 9); the executions app registers a callback here instead.  A hook runs
with the assignment row already locked and may raise to refuse the operation (the whole transaction rolls back).
"""

from __future__ import annotations

from typing import Any, Callable

#: ``hook(assignment, actor, reason)`` - called by ``lifecycle.cancel`` after the checks, before the status changes.
before_cancel: list[Callable[[Any, Any, str], None]] = []


def register_before_cancel(hook: Callable[[Any, Any, str], None]) -> None:
    if hook not in before_cancel:
        before_cancel.append(hook)
