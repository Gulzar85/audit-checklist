"""Controlled integration points for Phase 13 (corrective actions) and later verification - registered by those apps, never edited here.

``closure_blockers``: callables ``blocker(finding) -> str | None``.  A returned message blocks ``close`` (e.g. "2 corrective actions are
still open").  With ``FINDINGS_REQUIRE_VERIFICATION`` on and no ``verification_gate`` registered, closing is blocked: Phase 12 never
pretends a finding was independently verified.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: ``guard(finding, transition_name, actor) -> str | None``: a message refuses a lifecycle transition (Phase 14 refuses a direct call while a workflow governs it).
transition_guards: list[Callable[[Any, str, Any], str | None]] = []
closure_blockers: list[Callable[[Any], str | None]] = []
#: Same contract for ``resolve`` and ``cancel`` (Phase 13 registers the corrective-action policy: nothing is resolved or cancelled out from under live work).
resolve_blockers: list[Callable[[Any], str | None]] = []
cancel_blockers: list[Callable[[Any], str | None]] = []
#: ``gate(finding) -> bool``: has an independent verification accepted this finding?  Registered by the verification phase.
verification_gate: list[Callable[[Any], bool]] = []


def register_transition_guard(guard: Callable[[Any, str, Any], str | None]) -> None:
    if guard not in transition_guards:
        transition_guards.append(guard)


def register_closure_blocker(blocker: Callable[[Any], str | None]) -> None:
    if blocker not in closure_blockers:
        closure_blockers.append(blocker)


def register_resolve_blocker(blocker: Callable[[Any], str | None]) -> None:
    if blocker not in resolve_blockers:
        resolve_blockers.append(blocker)


def register_cancel_blocker(blocker: Callable[[Any], str | None]) -> None:
    if blocker not in cancel_blockers:
        cancel_blockers.append(blocker)


def register_verification_gate(gate: Callable[[Any], bool]) -> None:
    if gate not in verification_gate:
        verification_gate.append(gate)
