"""The findings management commands: detect_findings (idempotent backfill) and seed_demo_findings."""

from __future__ import annotations

from io import StringIO
from unittest import mock

import pytest
from django.core.management import CommandError, call_command

from apps.findings.models import Finding
from apps.findings.services import detection

from findings_support import build_findings_world, submit


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_findings_world(org_a, org_b, make_user, assign, make_type)


def run(*args, **kw):
    out = StringIO()
    call_command(*args, stdout=out, stderr=StringIO(), **kw)
    return out.getvalue()


@pytest.mark.django_db
class TestDetectFindings:
    def test_it_raises_what_a_failed_detection_missed_and_is_idempotent(self, w):
        with mock.patch.object(detection, "detect_for_execution", side_effect=RuntimeError("boom")):
            ex = submit(w, {"EXIT": "no"})
        assert not Finding.objects.exists()
        assert "raised 1 finding" in run("detect_findings", organization="org-a")
        assert Finding.objects.filter(execution=ex).count() == 1
        assert "raised 0 finding" in run("detect_findings")  # a second run changes nothing
        assert Finding.objects.filter(execution=ex).count() == 1

    def test_dry_run_writes_nothing_and_options_are_validated(self, w):
        with mock.patch.object(detection, "detect_for_execution", side_effect=RuntimeError("boom")):
            ex = submit(w, {"EXIT": "no"})
        assert "would be raised" in run("detect_findings", execution=f"EXE-{ex.number:05d}", dry_run=True)
        assert not Finding.objects.exists()
        for bad in ({"execution": "nope"}, {"since": "yesterday"}):
            with pytest.raises(CommandError):
                run("detect_findings", **bad)


@pytest.mark.django_db
class TestSeedDemo:
    def test_the_demo_is_complete_and_idempotent(self, w, org_a):
        out = run("seed_demo_findings", organization=org_a.slug)
        assert "Findings demo DEMO-FND created" in out
        statuses = set(Finding.objects.filter(organization=org_a).values_list("status", flat=True))
        assert "resolved" in statuses and Finding.objects.filter(organization=org_a, source_type="manual").count() == 1
        assert Finding.objects.filter(organization=org_a, source_type="checklist_rule").count() == 3
        before = Finding.objects.count()
        assert "already present" in run("seed_demo_findings", organization=org_a.slug)
        assert Finding.objects.count() == before
