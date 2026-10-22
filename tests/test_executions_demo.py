"""The execution demo command: idempotent, generic, goes through the real services."""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.executions.models import ChecklistExecution, ChecklistResponse

from assignments_support import build_world


@pytest.mark.django_db
def test_seed_demo_executions_is_idempotent_and_complete(org_a, org_b, make_user, assign, make_type):
    build_world(org_a, org_b, make_user, assign, make_type)
    call_command("seed_demo_executions", organization=org_a.slug)
    rows = {e.assignment.location.code: e for e in ChecklistExecution.objects.filter(organization=org_a).select_related("assignment__location")}
    assert set(rows) == {"DEMO-R-1", "DEMO-WH-1"}
    assert rows["DEMO-R-1"].status == "in_progress" and rows["DEMO-WH-1"].status == "submitted"
    assert rows["DEMO-WH-1"].assignment.status == "completed" and rows["DEMO-R-1"].assignment.status == "in_progress"
    assert rows["DEMO-R-1"].responses.count() == 2  # the out-of-range temperature was refused, not stored
    assert ChecklistResponse.objects.filter(execution=rows["DEMO-WH-1"]).count() == 5
    assert not ChecklistExecution.objects.filter(organization=org_b).exists()
    call_command("seed_demo_executions", organization=org_a.slug)
    assert ChecklistExecution.objects.filter(organization=org_a).count() == 2


@pytest.mark.django_db
def test_seed_demo_executions_needs_exactly_one_organization(org_a, org_b):
    with pytest.raises(CommandError):
        call_command("seed_demo_executions")
