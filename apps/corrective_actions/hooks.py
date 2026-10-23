"""Guards other phases may register on corrective-action lifecycle transitions (Phase 14 registers the workflow-governance guard).

``transition_guards``: callables ``guard(action, transition_name, actor) -> str | None``; a returned message refuses the transition (``governed_by_workflow``).
A guard runs after the permission check and before any state is changed, so it can only add a restriction.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

transition_guards: list[Callable[[Any, str, Any], str | None]] = []


def register_transition_guard(guard: Callable[[Any, str, Any], str | None]) -> None:
    if guard not in transition_guards:
        transition_guards.append(guard)
