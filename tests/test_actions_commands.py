"""The corrective-action demo command: complete, idempotent and never self-verifying."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from apps.corrective_actions.models import CorrectiveAction, CorrectiveActionVerification

from findings_support import build_findings_world


@pytest.mark.django_db
def test_the_demo_is_complete_idempotent_and_respects_separation_of_duties(org_a, org_b, make_user, assign, make_type):
    build_findings_world(org_a, org_b, make_user, assign, make_type)

    def run():
        out = StringIO()
        call_command("seed_demo_corrective_actions", organization=org_a.slug, stdout=out)
        return out.getvalue()

    first = run()
    assert "Corrective action demo created" in first
    actions = CorrectiveAction.objects.filter(organization=org_a)
    assert actions.count() == 3 and set(actions.values_list("status", flat=True)) >= {"in_progress", "draft"}
    for v in CorrectiveActionVerification.objects.filter(action__organization=org_a):
        assert v.verifier_id != v.action.assigned_to_id  # a decision is never made by the owner
    assert "already present" in run() and CorrectiveAction.objects.filter(organization=org_a).count() == 3
