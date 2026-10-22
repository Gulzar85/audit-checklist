"""Smoke: the start -> answer -> submit happy path through the services."""

from __future__ import annotations

import pytest

from apps.executions import services
from apps.executions.models import ChecklistExecution

from executions_support import REQUIRED, build_exec_world, issued, item_id


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_exec_world(org_a, org_b, make_user, assign, make_type)


@pytest.mark.django_db
def test_happy_path(w):
    a = issued(w)
    ex, created = services.start_or_resume(a, actor=w["runner"])
    assert created and ex.status == "in_progress"
    a.refresh_from_db()
    assert a.status == "in_progress"
    inputs = [services.AnswerInput(item_id(w["eversion"], c), v) for c, v in REQUIRED.items()]
    r = services.save_answers(ex, actor=w["runner"], inputs=inputs)
    assert r.ok and len(r.saved) == 3, (r.errors, r.conflicts)
    p = services.compute_progress(r.state)
    assert p["percent"] == 100
    done = services.submit(ex, actor=w["runner"])
    assert done.execution.status == "submitted"
    a.refresh_from_db()
    assert a.status == "completed"
    assert ChecklistExecution.objects.count() == 1
