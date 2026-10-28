"""Builders for the Phase 20 tests."""

from __future__ import annotations

from apps.permissions import services as rbac
from apps.permissions.models import MembershipRole
from apps.permissions.registry import P

from reporting_support import REPORTER, build_reporting_world, seed_executions  # noqa: F401
from workflows_support import build_workflow_world

DASH = [P.DASHBOARD_VIEW]


def add_dashboard_users(w, org, make_user, assign, prefix="db"):
    for key, perms, location in (("dash", [*REPORTER, *DASH], None), ("dash_manager", [*REPORTER, *DASH, P.DASHBOARD_MANAGE], None),
                                 ("dash_r1", [*REPORTER, *DASH], w["r1"]), ("dash_noview", REPORTER, None), ("dash_findings_only", [P.REPORT_VIEW, P.LOCATION_VIEW, P.FINDING_VIEW, *DASH], None)):
        role = rbac.create_role(organization=org, name=f"{prefix} {key}", permissions=perms)
        user = make_user(email=f"{prefix}-{key}@example.com", organization=org, role=None)
        if location is None:
            assign(user, org, role)
        else:
            assign(user, org, role, scope=MembershipRole.ScopeType.LOCATION, location=location)
        w[key] = user
    return w


def build_exec_world(org_a, org_b, make_user, assign, make_type):
    w = build_reporting_world(org_a, org_b, make_user, assign, make_type)
    add_dashboard_users(w, org_a, make_user, assign)
    role_b = rbac.create_role(organization=org_b, name="db other", permissions=[*REPORTER, *DASH])
    other = make_user(email="db-other@example.com", organization=org_b, role=None)
    assign(other, org_b, role_b)
    w["dash_other"] = other
    return w


def build_quality_world(org_a, org_b, make_user, assign, make_type):
    w = build_workflow_world(org_a, org_b, make_user, assign, make_type)
    w.setdefault("org", org_a)
    return add_dashboard_users(w, org_a, make_user, assign, prefix="dq")
