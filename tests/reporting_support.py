"""Builders for the Phase 19 tests: the evaluation world (assignments, executions, scores, evaluations over a location tree) plus reporting users."""

from __future__ import annotations

from apps.permissions.models import MembershipRole
from apps.permissions import services as rbac
from apps.permissions.registry import P

from evaluation_support import FIRE_NO, GOOD, build_evaluation_world, publish_evaluated, run

SOURCES = [P.LOCATION_VIEW, P.EXECUTION_VIEW, P.ASSIGNMENT_VIEW, P.SCORING_SCORE_VIEW, P.EVALUATION_RESULT_VIEW, P.FINDING_VIEW, P.CORRECTIVE_ACTION_VIEW, P.WORKFLOW_INSTANCE_VIEW]
REPORTER = [P.REPORT_VIEW, *SOURCES]


def build_reporting_world(org_a, org_b, make_user, assign, make_type):
    w = build_evaluation_world(org_a, org_b, make_user, assign, make_type)
    specs = {"reporter": ([*REPORTER, P.REPORT_EXPORT, P.REPORT_PEOPLE, P.REPORT_SHARE], None), "viewer_only": (REPORTER, None), "exporter": ([*REPORTER, P.REPORT_EXPORT], None),
             "r1_reporter": ([*REPORTER, P.REPORT_EXPORT, P.REPORT_PEOPLE], w["r1"]), "no_report": (SOURCES, None), "other_reporter": (None, None)}
    for key, (perms, location) in specs.items():
        if perms is None:
            continue
        role = rbac.create_role(organization=org_a, name=f"Reports {key}", permissions=perms)
        user = make_user(email=f"rp-{key}@example.com", organization=org_a, role=None)
        if location is None:
            assign(user, org_a, role)
        else:
            assign(user, org_a, role, scope=MembershipRole.ScopeType.LOCATION, location=location)
        w[key] = user
    role_b = rbac.create_role(organization=org_b, name="Reports B", permissions=[*REPORTER, P.REPORT_EXPORT, P.REPORT_PEOPLE])
    other = make_user(email="rp-other@example.com", organization=org_b, role=None)
    assign(other, org_b, role_b)
    w["other_reporter"] = other
    return w


def seed_executions(w):
    """Four executions by the Area 1 runner: r1 good (submitted), r2 failing a critical item and r2 good (submitted), and one in progress at r1."""
    version = publish_evaluated(w)
    good1 = run(w, version, GOOD, who="area_runner", location=w["r1"])
    bad2 = run(w, version, {**GOOD, **FIRE_NO}, who="area_runner", location=w["r2"])
    good3 = run(w, version, GOOD, who="area_runner", location=w["r2"])
    open1 = run(w, version, None, who="area_runner", location=w["r1"], submit=False)
    return version, good1, bad2, good3, open1
