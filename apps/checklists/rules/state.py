"""The state of one source answer as the rule engine sees it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Source-answer statuses used by conditions.
ANSWERED = "answered"  # a valid, normalized answer
EMPTY = "empty"  # nothing answered (missing or blank) - NOT the same as "No" or 0
NOT_APPLICABLE = "not_applicable"  # explicitly declared N/A
INVALID = "invalid"  # an answer that fails the answer-type validation
HIDDEN = "hidden"  # the item (or its section) is currently hidden by rules: its answer is ignored, not deleted


@dataclass(frozen=True)
class AnswerState:
    status: str
    value: Any = None  # normalized (Phase 5) value when ``answered``
    serialized: Any = None
    errors: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "value": self.serialized, "errors": list(self.errors)}
