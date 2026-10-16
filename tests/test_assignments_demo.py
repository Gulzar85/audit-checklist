"""The assignment demo command: idempotent, generic, goes through the lifecycle services."""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.assignments.models import ChecklistAssignment
from apps.core.models import ActivityLog

from assignments_support import build_world


@pytest.mark.django_db
def test_seed_demo_assignments_is_idempotent_and_complete(org_a, org_b, make_user, assign, make_type):
    build_world(org_a, org_b, make_user, assign, make_type)
    call_command("seed_demo_assignments", organization=org_a.slug)
    rows = {a.location.code: a for a in ChecklistAssignment.objects.filter(organization=org_a).select_related("location")}
    assert set(rows) == {"DEMO-R-1", "DEMO-R-2", "DEMO-R-3", "DEMO-WH-1"}
    assert rows["DEMO-R-1"].status == "assigned" and rows["DEMO-R-2"].priority == "critical" and rows["DEMO-WH-1"].location.location_type.name == "Warehouse"
    assert rows["DEMO-R-3"].status == "cancelled" and rows["DEMO-R-3"].cancel_reason
    assert ActivityLog.objects.filter(verb="assignment.cancelled").exists() and ActivityLog.objects.filter(verb="assignment.issued").count() == 4
    assert not ChecklistAssignment.objects.filter(organization=org_b).exists()
    call_command("seed_demo_assignments", organization=org_a.slug)  # again: nothing new
    assert ChecklistAssignment.objects.filter(organization=org_a).count() == 4


@pytest.mark.django_db
def test_seed_demo_assignments_needs_exactly_one_organization(org_a, org_b):
    with pytest.raises(CommandError):
        call_command("seed_demo_assignments")
