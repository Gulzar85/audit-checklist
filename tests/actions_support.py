"""Builders for the Phase 13 tests: the findings world plus corrective-action users, findings and actions at each lifecycle stage."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from apps.corrective_actions import services
from apps.findings import services as finding_services
from apps.findings.models import FindingCategory, FindingSeverity
from apps.permissions import services as rbac
from apps.permissions.models import MembershipRole
from apps.permissions.registry import P

from findings_support import build_findings_world

WORKER = [P.CORRECTIVE_ACTION_VIEW, P.CORRECTIVE_ACTION_START, P.CORRECTIVE_ACTION_UPDATE_PROGRESS, P.CORRECTIVE_ACTION_BLOCK, P.CORRECTIVE_ACTION_SUBMIT, P.LOCATION_VIEW]
MANAGER = [P.CORRECTIVE_ACTION_VIEW, P.CORRECTIVE_ACTION_CREATE, P.CORRECTIVE_ACTION_UPDATE, P.CORRECTIVE_ACTION_ASSIGN, P.CORRECTIVE_ACTION_REASSIGN,
           P.CORRECTIVE_ACTION_CANCEL, P.CORRECTIVE_ACTION_REOPEN, P.CORRECTIVE_ACTION_CLOSE, P.FINDING_VIEW, P.FINDING_RESOLVE, P.LOCATION_VIEW]
VERIFIER = [P.CORRECTIVE_ACTION_VIEW, P.CORRECTIVE_ACTION_VERIFY, P.CORRECTIVE_ACTION_REJECT, P.LOCATION_VIEW]
VIEWER = [P.CORRECTIVE_ACTION_VIEW, P.LOCATION_VIEW]
#: Someone who can both do the work and verify it - used to prove separation of duties.
BOTH = [*WORKER, P.CORRECTIVE_ACTION_VERIFY, P.CORRECTIVE_ACTION_REJECT]


def future(days: int = 3):  # noqa: ANN201
    return timezone.now() + timedelta(days=days)


def build_actions_world(org_a, org_b, make_user, assign, make_type):
    w = build_findings_world(org_a, org_b, make_user, assign, make_type)
    for name, perms, who in (("Action worker", WORKER, "worker1"), ("Action worker 2", WORKER, "worker2"), ("Action manager", MANAGER, "manager"),
                             ("Action verifier", VERIFIER, "verifier"), ("Action viewer", VIEWER, "aviewer"), ("Action both", BOTH, "both")):
        role = rbac.create_role(organization=org_a, name=name, permissions=perms)
        user = make_user(email=f"ca-{who}@example.com", organization=org_a, role=None)
        assign(user, org_a, role, scope=MembershipRole.ScopeType.LOCATION, location=w["r1"])
        w[who] = user
    acting = rbac.create_role(organization=org_a, name="Acting manager", permissions=[
        P.CORRECTIVE_ACTION_VIEW, P.CORRECTIVE_ACTION_UPDATE, P.CORRECTIVE_ACTION_START, P.CORRECTIVE_ACTION_UPDATE_PROGRESS, P.CORRECTIVE_ACTION_SUBMIT,
        P.CORRECTIVE_ACTION_BLOCK, P.LOCATION_VIEW])
    w["acting"] = make_user(email="ca-acting@example.com", organization=org_a, role=None)
    assign(w["acting"], org_a, acting, scope=MembershipRole.ScopeType.LOCATION, location=w["r1"])
    elsewhere = rbac.create_role(organization=org_a, name="Action elsewhere", permissions=[P.CORRECTIVE_ACTION_VIEW, P.CORRECTIVE_ACTION_VERIFY, P.LOCATION_VIEW])
    w["r2_actor"] = make_user(email="ca-r2@example.com", organization=org_a, role=None)
    assign(w["r2_actor"], org_a, elsewhere, scope=MembershipRole.ScopeType.LOCATION, location=w["r2"])
    w["b_admin"] = make_user(email="ca-b-admin@example.com", organization=org_b, role="administrator")
    finding_services.ensure_defaults(org_b)
    return w


def make_finding(w, *, location=None, title="Unsafe equipment", severity="high"):
    if location is not None and location != w["r1"]:
        severity = "medium"  # the triager (a possible owner of high findings) is scoped to Restaurant 1
    return finding_services.create_manual(
        actor=w["admin"], location=location or w["r1"], title=title, category=FindingCategory.objects.get(organization=w["org_a"], code="equipment"),
        severity=FindingSeverity.objects.get(organization=w["org_a"], code=severity), assigned_to=w["triager"] if severity in ("high", "critical") else None)


def make_action(w, finding=None, *, who="manager", owner="worker1", issue=True, **kw):
    finding = finding or make_finding(w)
    kw.setdefault("title", "Repair the equipment")
    return services.create_action(actor=w[who], finding=finding, assigned_to=w[owner] if owner else None, issue=issue, **kw)


def to_status(w, action, status, *, owner="worker1", verifier="verifier"):
    """Walk an action through the real services to ``status``."""
    steps = {
        "open": [],
        "in_progress": [(owner, "start", "")],
        "blocked": [(owner, "start", ""), (owner, "block", "Waiting for a part")],
        "submitted": [(owner, "start", ""), (owner, "submit", "Replaced the part and tested it.")],
        "rejected": [(owner, "start", ""), (owner, "submit", "Done."), (verifier, "reject", "Not tested under load.")],
        "verified": [(owner, "start", ""), (owner, "submit", "Done."), (verifier, "verify", "Checked on site.")],
        "closed": [(owner, "start", ""), (owner, "submit", "Done."), (verifier, "verify", ""), ("manager", "close", "")],
    }[status]
    for who, name, reason in steps:
        action = services.transition(action, name, actor=w[who], reason=reason)
    return action
