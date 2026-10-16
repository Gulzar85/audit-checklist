"""Bulk assignment: one published version to many locations in one operation.

Policy (ADR 0051): **all-or-nothing**.  Every target is validated first (permission at that location, organization,
activity, version eligibility, duplicates, responsible user); if *any* target fails, nothing is created and the result lists, per
location, whether it would have succeeded and why not - so a misleading "all created" is impossible and a partial result
never exists.  Repeating a submission is safe: targets that already have an open (or draft) assignment of the version are
reported as duplicates and the whole request is refused.  Locations the caller may not use, unknown ids and other
organizations' locations all produce the same generic "not available" entry - nothing about them is disclosed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _

from apps.core.utils import parse_uuid
from apps.locations.models import Location

from .. import policies
from ..constants import AssignmentPriority
from ..models import ChecklistAssignment
from . import validation as v
from .assignment import VERBS, create_assignment, lock_organization, log, next_number
from .errors import AssignmentError

MAX_TARGETS = 200


@dataclass
class TargetResult:
    location_id: str
    name: str = ""
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    assignment: ChecklistAssignment | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"location": self.location_id, "name": self.name, "ok": self.ok, "errors": self.errors}
        if self.assignment is not None:
            out["assignment"] = str(self.assignment.pk)
            out["reference"] = self.assignment.reference
        return out


@dataclass
class BulkResult:
    created: bool
    results: list[TargetResult]
    issued: bool = False

    @property
    def count(self) -> int:
        return sum(1 for r in self.results if r.assignment is not None)

    def as_dict(self) -> dict[str, Any]:
        return {"created": self.created, "issued": self.issued, "count": self.count,
                "succeeded": sum(1 for r in self.results if r.ok), "failed": sum(1 for r in self.results if not r.ok),
                "results": [r.as_dict() for r in self.results]}


def _resolve(actor: Any, version: Any, location_ids: list[Any]) -> tuple[list[tuple[str, Location | None]], list[TargetResult]]:
    """Resolve submitted ids to locations the actor may bulk-assign to; everything else becomes a generic refusal."""
    ids = []
    for raw in location_ids:
        parsed = parse_uuid(raw)
        ids.append((str(raw), parsed))
    found = {loc.pk: loc for loc in Location.objects.filter(pk__in=[p for _r, p in ids if p]).select_related("organization")}
    pairs: list[tuple[str, Location | None]] = []
    for raw, parsed in ids:
        location = found.get(parsed) if parsed else None
        if location is not None and not policies.can_bulk_create(actor, location):
            location = None  # out of scope: indistinguishable from "does not exist"
        pairs.append((raw, location))
    return pairs, []


def bulk_create_assignments(
    *, actor: Any, version: Any, location_ids: list[Any], priority: str = AssignmentPriority.NORMAL, due_at: Any = None,
    instructions: str = "", assigned_to: Any = None, issue: bool = False, dry_run: bool = False,
) -> BulkResult:
    """Create assignments for ``location_ids`` (all-or-nothing).  ``dry_run`` only reports what would happen."""
    unique_ids = list(dict.fromkeys(str(i) for i in location_ids))
    if not unique_ids:
        raise AssignmentError(_("Choose at least one location."), "no_targets")
    if len(unique_ids) > MAX_TARGETS:
        raise AssignmentError(_("At most %(n)s locations may be assigned at once.") % {"n": MAX_TARGETS}, "too_many_targets")
    pairs, _unused = _resolve(actor, version, unique_ids)
    results = _preflight(actor, version, pairs, priority=priority, due_at=due_at, instructions=instructions,
                         assigned_to=assigned_to, issue=issue)
    if dry_run or any(not r.ok for r in results):
        return BulkResult(False, results, issue)
    try:
        with transaction.atomic():
            created: list[ChecklistAssignment] = []
            for (raw, location), result in zip(pairs, results):
                assert location is not None
                lock_organization(location.organization_id)
                result.assignment = create_assignment(
                    actor=actor, version=version, location=location, priority=priority, due_at=due_at,
                    instructions=instructions, assigned_to=assigned_to, issue=issue,
                )
                created.append(result.assignment)
            first = created[0]
            log(VERBS["bulk_created"], first, actor, f"Bulk-created {len(created)} assignment(s)", count=len(created),
                references=[a.reference for a in created][:50], version=version.version_number, issued=issue)
    except (AssignmentError, PermissionDenied, IntegrityError) as exc:
        # A race slipped past the preflight: nothing was created; report again, per location, from fresh data.
        results = _preflight(actor, version, pairs, priority=priority, due_at=due_at, instructions=instructions,
                             assigned_to=assigned_to, issue=issue)
        if all(r.ok for r in results):
            message = " ".join(getattr(exc, "messages", [])) or str(_("The request conflicted with another change. Try again."))
            results[0].ok = False
            results[0].errors.append(message)
        return BulkResult(False, results, issue)
    return BulkResult(True, results, issue)


def _preflight(actor: Any, version: Any, pairs: list[tuple[str, Location | None]], *, priority: str, due_at: Any, instructions: str,
               assigned_to: Any, issue: bool) -> list[TargetResult]:
    results: list[TargetResult] = []
    seen_orgs: set[Any] = set()
    for raw, location in pairs:
        if location is None:
            results.append(TargetResult(raw, _("(not available)"), False, [str(_("This location is not available to you."))]))
            continue
        result = TargetResult(str(location.pk), location.display_path)
        results.append(result)
        try:
            org = location.organization
            seen_orgs.add(org.pk)
            if not policies.can_use_checklist(actor, org):
                raise PermissionDenied
            if issue and not policies.can_issue_at(actor, location):
                result.ok = False
                result.errors.append(str(_("You may create but not issue assignments at this location.")))
                continue
            v.validate_target(organization=org, version=version, location=location)
            v.validate_due(due_at)
            v.validate_assignee(user=assigned_to, organization=org, location=location)
            v.check_duplicate(version, location, include_drafts=True)
        except PermissionDenied:
            result.ok, result.errors = False, [str(_("This location is not available to you."))]
        except AssignmentError as exc:
            result.ok, result.errors = False, [str(m) for m in exc.messages]
    if priority not in AssignmentPriority.values:
        for r in results:
            r.ok = False
            r.errors.append(str(_("Unknown priority.")))
    if len(seen_orgs) > 1:
        for r in results:
            r.ok = False
            r.errors.append(str(_("All locations must belong to one organization.")))
    return results


__all__ = ["BulkResult", "MAX_TARGETS", "TargetResult", "bulk_create_assignments", "next_number"]
