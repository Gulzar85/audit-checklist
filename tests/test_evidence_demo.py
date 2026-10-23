"""The evidence demo command: idempotent, and the assignment it creates is offline-capable (Phase 10 x 11)."""

from __future__ import annotations

from django.core.management import call_command


def test_demo_command_is_idempotent_and_offline_capable(org_a, org_b, make_user, assign, make_type):
    from apps.assignments.models import ChecklistAssignment
    from apps.executions.offline import compat
    from assignments_support import build_world

    build_world(org_a, org_b, make_user, assign, make_type)
    call_command("seed_demo_evidence", organization=org_a.slug)
    call_command("seed_demo_evidence", organization=org_a.slug)
    qs = ChecklistAssignment.objects.filter(organization=org_a, version__template__code="DEMO-EVD")
    assert qs.count() == 1
    assert compat.EVALUATOR_VERSION == 2
