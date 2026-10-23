"""The parent-finding policy (ADR 0068): how a finding's corrective actions affect its resolution and closure.

A finding is **eligible** for resolution when none of its corrective actions is still *outstanding* (draft, open, in progress, blocked,
awaiting verification or rejected).  Cancelled actions do not count; verified and closed ones are done.  A finding with no actions is
eligible - actions are optional unless an organization's process says otherwise (``CORRECTIVE_ACTIONS_REQUIRED_FOR_VERIFICATION``).

This is a **gate, never an automation**: nothing here (or anywhere in this app) resolves, closes or cancels a finding.  The module
registers the policy with the Phase 12 finding lifecycle through its documented hooks (``integration.py``), so the finding service
itself refuses to resolve while work is outstanding and a person still performs every finding transition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from .constants import DONE_STATUSES, OUTSTANDING_STATUSES, ActionStatus
from .models import CorrectiveAction


@dataclass
class Eligibility:
    eligible: bool
    outstanding: list[str] = field(default_factory=list)  # references
    done: int = 0
    cancelled: int = 0
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "outstanding": self.outstanding, "done": self.done, "cancelled": self.cancelled, "reasons": self.reasons}


def finding_eligibility(finding: Any) -> Eligibility:
    """Evaluate *all* of the finding's actions (not only the ones a given user may see: eligibility is a fact about the finding)."""
    rows = list(CorrectiveAction.objects.filter(finding=finding).values_list("number", "status"))
    outstanding = [f"CA-{n:05d}" for n, s in rows if s in OUTSTANDING_STATUSES]
    done = sum(1 for _n, s in rows if s in DONE_STATUSES)
    cancelled = sum(1 for _n, s in rows if s == ActionStatus.CANCELLED)
    reasons: list[str] = []
    if outstanding:
        reasons.append(f"{len(outstanding)} corrective action(s) still outstanding: {', '.join(outstanding[:5])}" + (" ..." if len(outstanding) > 5 else ""))
    return Eligibility(not outstanding, outstanding, done, cancelled, reasons)


def resolution_blocker(finding: Any) -> str | None:
    """Phase 12 ``resolve_blockers`` hook: a message when outstanding actions block resolving the finding."""
    if not getattr(settings, "CORRECTIVE_ACTIONS_BLOCK_FINDING_RESOLUTION", True):
        return None
    result = finding_eligibility(finding)
    return None if result.eligible else "; ".join(result.reasons)


def cancellation_blocker(finding: Any) -> str | None:
    """Phase 12 ``cancel_blockers`` hook: a finding is never cancelled out from under live work - cancel the actions first."""
    result = finding_eligibility(finding)
    if result.outstanding:
        return f"Cancel or finish its corrective actions first ({', '.join(result.outstanding[:5])})."
    return None


def verification_gate(finding: Any) -> bool:
    """Phase 12 ``verification_gate``: the finding counts as independently verified when work was done, verified, and nothing is outstanding."""
    result = finding_eligibility(finding)
    if not result.eligible:
        return False
    if getattr(settings, "CORRECTIVE_ACTIONS_REQUIRED_FOR_VERIFICATION", True):
        return result.done > 0
    return True
