"""Controlled integration points for Phase 13 (corrective actions) and later verification - registered by those apps, never edited here.

``closure_blockers``: callables ``blocker(finding) -> str | None``.  A returned message blocks ``close`` (e.g. "2 corrective actions are
still open").  With ``FINDINGS_REQUIRE_VERIFICATION`` on and no ``verification_gate`` registered, closing is blocked: Phase 12 never
pretends a finding was independently verified.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

closure_blockers: list[Callable[[Any], str | None]] = []
#: ``gate(finding) -> bool``: has an independent verification accepted this finding?  Registered by the verification phase.
verification_gate: list[Callable[[Any], bool]] = []


def register_closure_blocker(blocker: Callable[[Any], str | None]) -> None:
    if blocker not in closure_blockers:
        closure_blockers.append(blocker)


def register_verification_gate(gate: Callable[[Any], bool]) -> None:
    if gate not in verification_gate:
        verification_gate.append(gate)
