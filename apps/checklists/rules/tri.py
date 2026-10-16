"""Three-valued (Kleene) logic for rule conditions.

``UNKNOWN`` means "the data needed to decide is not there (yet)" - an unanswered, N/A, invalid or hidden source.  It is
never silently turned into FALSE (an unanswered question is not "No") and never into TRUE.  Only ``TRUE`` triggers a
rule's actions.

========  ==========================================  ==========================================
          AND                                          OR
========  ==========================================  ==========================================
rule      any FALSE -> FALSE; else any UNKNOWN ->      any TRUE -> TRUE; else any UNKNOWN ->
          UNKNOWN; else TRUE                           UNKNOWN; else FALSE
========  ==========================================  ==========================================
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum


class Tri(str, Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"

    @classmethod
    def of(cls, value: bool) -> Tri:
        return cls.TRUE if value else cls.FALSE


def tri_and(values: Iterable[Tri]) -> Tri:
    seen_unknown = False
    for value in values:
        if value is Tri.FALSE:
            return Tri.FALSE
        seen_unknown |= value is Tri.UNKNOWN
    return Tri.UNKNOWN if seen_unknown else Tri.TRUE


def tri_or(values: Iterable[Tri]) -> Tri:
    seen_unknown = False
    for value in values:
        if value is Tri.TRUE:
            return Tri.TRUE
        seen_unknown |= value is Tri.UNKNOWN
    return Tri.UNKNOWN if seen_unknown else Tri.FALSE
