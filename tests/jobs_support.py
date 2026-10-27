"""Builders for the Phase 18 tests: two organizations, users with scheduling permissions, and a scriptable task."""

from __future__ import annotations

from apps.jobs import registry
from apps.jobs.errors import PermanentError, RetryableError
from apps.permissions import services as rbac
from apps.permissions.registry import P

CALLS: list[dict] = []
SCRIPT: list = []


def handler(ctx):
    """Test handler: records the call, then raises / returns the next scripted outcome (an Exception is raised, a dict is the result)."""
    CALLS.append({"org": ctx.organization, "payload": dict(ctx.payload), "job": ctx.job_id})
    step = SCRIPT.pop(0) if SCRIPT else {"ok": 1}
    if isinstance(step, BaseException):
        raise step
    return step


def reset(*steps):
    CALLS.clear()
    SCRIPT.clear()
    SCRIPT.extend(steps)


def install_test_tasks():
    for code, scope in (("test.echo", registry.SCOPE_BOTH), ("test.org_only", registry.SCOPE_ORGANIZATION), ("test.global_only", registry.SCOPE_GLOBAL)):
        if code not in registry.REGISTRY:
            registry.register(registry.Task(code, code, "test task", scope, handler, {"n": registry.Field("int", 1, 0, 10)}, max_attempts=3, base_delay_seconds=10, max_delay_seconds=100))


def build_jobs_world(org_a, org_b, make_user, assign, superuser):
    install_test_tasks()
    w = {"org_a": org_a, "org_b": org_b, "super": superuser}
    for key, perms in (("manager", [P.JOBS_VIEW, P.JOBS_MANAGE, P.JOBS_RUN]), ("viewer", [P.JOBS_VIEW]), ("runner_only", [P.JOBS_VIEW, P.JOBS_RUN]), ("nobody", [P.LOCATION_VIEW])):
        role = rbac.create_role(organization=org_a, name=f"Jobs {key}", permissions=perms)
        user = make_user(email=f"jobs-{key}@example.com", organization=org_a, role=None)
        assign(user, org_a, role)
        w[key] = user
    role_b = rbac.create_role(organization=org_b, name="Jobs manager B", permissions=[P.JOBS_VIEW, P.JOBS_MANAGE, P.JOBS_RUN])
    other = make_user(email="jobs-other@example.com", organization=org_b, role=None)
    assign(other, org_b, role_b)
    w["other"] = other
    return w
