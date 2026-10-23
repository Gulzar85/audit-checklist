"""Phase 12 REST API: list/filter/sort, manual creation, lifecycle actions, assignment, history, evidence, configuration, isolation."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.evidence import lifecycle as evidence_lifecycle
from apps.evidence import services as evidence_services
from apps.evidence.scanning import ScanResult
from apps.executions import services as exec_services
from apps.findings import services
from apps.findings.models import Finding, FindingCategory, FindingSeverity

from evidence_support import evidence_storage, jpeg_bytes  # noqa: F401
from executions_support import issued
from findings_support import build_findings_world, fid, submit
from test_evidence_web_api import api
from test_findings_lifecycle import cat, manual, sev

API = "/api/v1/"


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    world = build_findings_world(org_a, org_b, make_user, assign, make_type)
    world["b_admin"] = make_user(email="f-b-admin@example.com", organization=org_b, role="administrator")
    services.ensure_defaults(org_b)
    return world


def payload(w, **kw):
    return {"location": str(w["r1"].pk), "title": "Wet floor", "category": str(cat(w).pk), "severity": str(sev(w).pk), **kw}


class TestListAndFilters:
    def test_list_is_scoped_paginated_and_filterable(self, w):
        a = manual(w, who="admin", title="Alpha")
        b = manual(w, who="admin", title="Beta", severity=sev(w, "low"), location=w["r2"])
        c = api(w["admin"]).get(API + "findings/")
        assert c.status_code == 200 and {r["id"] for r in c.json()["results"]} == {str(a.pk), str(b.pk)}
        assert [r["id"] for r in api(w["viewer"]).get(API + "findings/").json()["results"]] == [str(a.pk)]
        assert api(w["b_admin"]).get(API + "findings/").json()["results"] == []
        by_sev = api(w["admin"]).get(API + "findings/", {"severity": str(sev(w, "low").pk)}).json()["results"]
        assert [r["id"] for r in by_sev] == [str(b.pk)]
        assert [r["title"] for r in api(w["admin"]).get(API + "findings/", {"search": "Alp"}).json()["results"]] == ["Alpha"]
        ordered = api(w["admin"]).get(API + "findings/", {"ordering": "severity__rank"}).json()["results"]
        assert [r["title"] for r in ordered] == ["Beta", "Alpha"]

    def test_the_overdue_filter_is_derived_from_the_clock(self, w):
        late, fine = manual(w, who="admin", title="Late"), manual(w, who="admin", title="Fine", due_at=timezone.now() + timedelta(days=5))
        Finding.objects.filter(pk=late.pk).update(due_at=timezone.now() - timedelta(days=1))
        rows = api(w["admin"]).get(API + "findings/", {"status": "overdue"}).json()["results"]
        assert [r["title"] for r in rows] == ["Late"] and rows[0]["is_overdue"] is True and rows[0]["effective_status"] == "overdue" and rows[0]["status"] == "open"
        services.transition(late, "cancel", actor=w["admin"], reason="Error")
        assert api(w["admin"]).get(API + "findings/", {"status": "overdue"}).json()["results"] == []  # finished work is never overdue
        _ = fine

    def test_filters_cannot_name_things_outside_the_scope(self, w):
        manual(w, who="admin")
        foreign = FindingSeverity.objects.get(organization=w["org_b"], code="low")
        r = api(w["viewer"]).get(API + "findings/", {"severity": str(foreign.pk)})
        assert r.status_code == 400  # not a valid choice for this caller: it is not even acknowledged to exist

    def test_summary_is_scoped(self, w):
        manual(w, who="admin"), manual(w, who="admin", location=w["r2"])
        assert api(w["viewer"]).get(API + "findings/summary/").json()["open"] == 1
        assert api(w["admin"]).get(API + "findings/summary/").json()["open"] == 2

    def test_authentication_is_required(self, w):
        assert api(None).get(API + "findings/").status_code in (401, 403)

    def test_a_user_without_any_finding_permission_is_refused(self, w, make_user):
        nobody = make_user(email="f-nobody-api@example.com", organization=w["org_a"], role=None)
        r = api(nobody).get(API + "findings/")
        assert r.status_code == 200 and r.json()["results"] == []


class TestCreate:
    def test_manual_creation(self, w):
        r = api(w["raiser"]).post(API + "findings/", payload(w, description="Near door"), format="json")
        body = r.json()
        assert r.status_code == 201 and body["reference"] == "FND-00001" and body["source_type"] == "manual" and body["status"] == "open"
        assert body["detected_by"] == str(w["raiser"].pk) and body["category"]["code"] == "safety" and body["severity"]["rank"] == 20
        assert body["source"] is None and body["allowed_actions"] == []

    def test_forged_fields_are_rejected_not_ignored(self, w):
        for field, value in (("organization", str(w["org_b"].pk)), ("status", "closed"), ("detected_by", str(w["admin"].pk)), ("source_type", "checklist_rule"),
                             ("number", 5), ("detection_key", "x"), ("rule_code", "x"), ("detected_at", "2020-01-01T00:00:00Z"), ("resolved_at", "2020-01-01T00:00:00Z")):
            r = api(w["raiser"]).post(API + "findings/", payload(w, **{field: value}), format="json")
            assert r.status_code == 400, field
        assert not Finding.objects.exists()

    def test_cross_organization_and_unknown_references_are_validation_errors(self, w):
        foreign_cat = FindingCategory.objects.get(organization=w["org_b"], code="safety")
        r = api(w["raiser"]).post(API + "findings/", payload(w, category=str(foreign_cat.pk)), format="json")
        assert r.status_code == 400 and "category" in str(r.json())
        r = api(w["raiser"]).post(API + "findings/", payload(w, location=str(w["r2"].pk)), format="json")  # a location the raiser can neither see nor use
        assert r.status_code == 400
        assert api(w["b_admin"]).post(API + "findings/", payload(w), format="json").status_code == 400  # Org A's location is "unknown" to Org B

    def test_permission_is_required_at_the_location(self, w):
        assert api(w["viewer"]).post(API + "findings/", payload(w), format="json").status_code == 403
        assert api(w["triager"]).post(API + "findings/", payload(w), format="json").status_code == 403

    def test_linking_an_execution_requires_visibility_of_it(self, w):
        ex = submit(w, {"EXIT": "yes"})
        r = api(w["raiser"]).post(API + "findings/", payload(w, execution=str(ex.pk)), format="json")
        assert r.status_code == 400 and "execution" in str(r.json())
        item = ex.version.sections.first().items.first()
        ok = api(w["admin"]).post(API + "findings/", payload(w, execution=str(ex.pk), item=str(item.pk)), format="json")
        assert ok.status_code == 201 and ok.json()["source"]["execution"] == str(ex.pk) and ok.json()["source"]["item"] == str(item.pk)

    def test_service_validation_is_structured(self, w):
        r = api(w["raiser"]).post(API + "findings/", payload(w, severity=str(sev(w, "high").pk)), format="json")  # high requires an owner
        assert r.status_code == 400 and r.json()["error"]["details"]["code"] == "owner_required"
        assert api(w["raiser"]).post(API + "findings/", payload(w, title=""), format="json").status_code == 400


class TestPatchAndAssign:
    def test_patch_changes_triage_fields_only(self, w):
        f = manual(w)
        r = api(w["editor"]).patch(API + f"findings/{f.pk}/", {"title": "New title", "severity": str(sev(w, "low").pk)}, format="json")
        assert r.status_code == 200 and r.json()["title"] == "New title" and r.json()["severity"]["code"] == "low"
        for field in ("status", "organization", "location", "execution", "detected_by", "assigned_to", "resolution_summary", "reference"):
            assert api(w["editor"]).patch(API + f"findings/{f.pk}/", {field: "x"}, format="json").status_code == 400, field
        assert api(w["editor"]).put(API + f"findings/{f.pk}/", {}, format="json").status_code == 405
        assert api(w["editor"]).delete(API + f"findings/{f.pk}/").status_code == 405

    def test_patch_needs_the_permission_and_an_editable_state(self, w):
        f = manual(w)
        assert api(w["viewer"]).patch(API + f"findings/{f.pk}/", {"title": "x"}, format="json").status_code == 403
        assert api(w["b_admin"]).patch(API + f"findings/{f.pk}/", {"title": "x"}, format="json").status_code == 404
        services.transition(f, "cancel", actor=w["editor"], reason="Error")
        assert api(w["editor"]).patch(API + f"findings/{f.pk}/", {"title": "x"}, format="json").status_code == 409

    def test_assign(self, w):
        f = manual(w)
        r = api(w["editor"]).post(API + f"findings/{f.pk}/assign/", {"assigned_to": str(w["triager"].pk)}, format="json")
        assert r.status_code == 200 and r.json()["assigned_to"]["email"] == w["triager"].email
        assert api(w["editor"]).post(API + f"findings/{f.pk}/assign/", {"assigned_to": str(w["viewer"].pk)}, format="json").status_code == 400
        assert api(w["editor"]).post(API + f"findings/{f.pk}/assign/", {"assigned_to": None}, format="json").json()["assigned_to"] is None
        assert api(w["triager"]).post(API + f"findings/{f.pk}/assign/", {"assigned_to": None}, format="json").status_code == 403
        assert api(w["editor"]).post(API + f"findings/{f.pk}/assign/", {"assigned_to": "00000000-0000-0000-0000-000000000000"}, format="json").status_code == 400


class TestLifecycleEndpoints:
    def test_the_full_cycle_and_allowed_actions(self, w):
        f = manual(w)
        t = api(w["triager"])
        assert t.get(API + f"findings/{f.pk}/").json()["allowed_actions"] == ["acknowledge", "start", "resolve"]
        assert t.post(API + f"findings/{f.pk}/acknowledge/").json()["status"] == "acknowledged"
        assert t.post(API + f"findings/{f.pk}/start/").json()["status"] == "in_progress"
        r = t.post(API + f"findings/{f.pk}/resolve/", {"reason": "Mopped and signed."}, format="json")
        assert r.json()["status"] == "resolved" and r.json()["resolution_summary"] == "Mopped and signed."
        e = api(w["editor"])
        assert e.post(API + f"findings/{f.pk}/close/").json()["status"] == "closed"
        assert e.post(API + f"findings/{f.pk}/reopen/", {"reason": "Leaking again"}, format="json").json()["status"] == "reopened"
        assert e.post(API + f"findings/{f.pk}/cancel/", {"reason": "Duplicate"}, format="json").json()["status"] == "cancelled"
        assert e.get(API + f"findings/{f.pk}/").json()["allowed_actions"] == []

    def test_errors_use_the_right_status_codes(self, w):
        f = manual(w)
        t = api(w["triager"])
        assert t.post(API + f"findings/{f.pk}/resolve/").status_code == 400  # a resolution summary is required
        assert t.post(API + f"findings/{f.pk}/close/").status_code == 403    # not theirs to do
        assert api(w["editor"]).post(API + f"findings/{f.pk}/close/").status_code == 400  # open -> closed is not a move
        t.post(API + f"findings/{f.pk}/acknowledge/")
        assert t.post(API + f"findings/{f.pk}/acknowledge/").status_code == 409  # a repeat
        assert api(w["b_admin"]).post(API + f"findings/{f.pk}/acknowledge/").status_code == 404
        assert api(w["viewer"]).post(API + f"findings/{f.pk}/acknowledge/").status_code == 403
        assert api(w["r2_viewer"]).post(API + f"findings/{f.pk}/acknowledge/").status_code == 404  # out of scope: not even visible
        assert api(None).post(API + f"findings/{f.pk}/acknowledge/").status_code in (401, 403)

    def test_status_cannot_be_written_by_any_payload(self, w):
        f = manual(w)
        r = api(w["editor"]).post(API + f"findings/{f.pk}/acknowledge/", {"status": "closed", "reason": "x"}, format="json")
        assert r.status_code == 400 and Finding.objects.get(pk=f.pk).status == "open"

    def test_history_lists_the_audit_trail(self, w):
        f = manual(w)
        services.transition(f, "acknowledge", actor=w["triager"])
        rows = api(w["viewer"]).get(API + f"findings/{f.pk}/history/").json()["results"]
        assert [r["verb"] for r in rows] == ["finding.acknowledged", "finding.created"]
        assert api(w["b_admin"]).get(API + f"findings/{f.pk}/history/").status_code == 404


class TestEvidence:
    def _finding_with_photo(self, w):
        ex, _ = exec_services.start_or_resume(issued(w, version=w["fversion"]), actor=w["runner"])
        att = evidence_services.upload(w["runner"], target_type="checklist_response", ref={"execution_id": str(ex.pk), "item_id": fid(w, "PHOTO")},
                                       filename="damage.jpg", data=jpeg_bytes()).attachment
        exec_services.save_answers(ex, actor=w["runner"], inputs=[exec_services.AnswerInput(fid(w, "EXIT"), "yes"), exec_services.AnswerInput(fid(w, "TAGS"), ["leak"])])
        done = exec_services.submit(ex, actor=w["runner"]).execution
        return Finding.objects.get(execution=done, rule_name="Leak damage"), att

    def test_the_finding_shows_the_triggering_responses_evidence(self, w):
        f, att = self._finding_with_photo(w)
        rows = api(w["viewer"]).get(API + f"findings/{f.pk}/evidence/").json()["results"]
        assert [r["id"] for r in rows] == [str(att.pk)] and rows[0]["available"] is True and rows[0]["name"] == "damage.jpg"
        assert "storage" not in str(rows).lower()
        d = api(w["viewer"]).get(API + f"findings/{f.pk}/evidence/{att.pk}/download/")
        assert d.status_code == 200 and d["X-Content-Type-Options"] == "nosniff" and d["Content-Disposition"].startswith("attachment") and d.content[:2] == b"\xff\xd8"
        assert api(w["viewer"]).get(API + f"findings/{f.pk}/evidence/{att.pk}/preview/")["Content-Disposition"].startswith("inline")

    def test_access_is_rechecked_for_the_finding_and_the_file(self, w):
        f, att = self._finding_with_photo(w)
        for user in ("b_admin", "r2_viewer"):
            assert api(w[user]).get(API + f"findings/{f.pk}/evidence/").status_code == 404
            assert api(w[user]).get(API + f"findings/{f.pk}/evidence/{att.pk}/download/").status_code == 404
        # another finding's evidence is never reachable through this one
        other = manual(w, who="admin")
        assert api(w["admin"]).get(API + f"findings/{other.pk}/evidence/{att.pk}/download/").status_code == 404
        assert api(w["admin"]).get(API + f"findings/{other.pk}/evidence/").json()["results"] == []

    def test_quarantined_and_unscanned_files_are_listed_but_never_served(self, w):
        f, att = self._finding_with_photo(w)
        evidence_lifecycle.apply_scan(att.asset, ScanResult("infected", "EICAR"))
        row = api(w["viewer"]).get(API + f"findings/{f.pk}/evidence/").json()["results"][0]
        assert row["status"] == "quarantined" and row["available"] is False and row["download_url"] is None
        assert api(w["viewer"]).get(API + f"findings/{f.pk}/evidence/{att.pk}/download/").status_code == 404

    def test_a_finding_that_does_not_inherit_evidence_shows_none(self, w):
        f, att = self._finding_with_photo(w)
        Finding.objects.filter(pk=f.pk).update(inherit_evidence=False)
        assert api(w["viewer"]).get(API + f"findings/{f.pk}/evidence/").json()["results"] == []
        assert api(w["viewer"]).get(API + f"findings/{f.pk}/evidence/{att.pk}/download/").status_code == 404


class TestConfigurationApi:
    def test_lists_are_organization_scoped(self, w):
        rows = api(w["viewer"]).get(API + "finding-categories/").json()["results"]
        assert {r["organization"] for r in rows} == {str(w["org_a"].pk)} and len(rows) == 6
        sevs = api(w["viewer"]).get(API + "finding-severities/").json()["results"]
        assert [s["code"] for s in sevs] == ["critical", "high", "medium", "low"]  # ordered by rank, not by name

    def test_writes_need_manage_in_that_organization(self, w):
        c = api(w["admin"])
        r = c.post(API + "finding-categories/", {"code": "hygiene", "name": "Hygiene"}, format="json")
        assert r.status_code == 201 and r.json()["organization"] == str(w["org_a"].pk) and r.json()["is_system"] is False
        assert api(w["editor"]).post(API + "finding-categories/", {"code": "x", "name": "X"}, format="json").status_code == 403
        assert api(w["viewer"]).patch(API + f"finding-categories/{r.json()['id']}/", {"name": "Y"}, format="json").status_code == 403
        assert c.post(API + "finding-categories/", {"code": "x", "name": "X", "organization": str(w["org_b"].pk)}, format="json").status_code == 400  # not their organization
        assert api(w["b_admin"]).patch(API + f"finding-categories/{r.json()['id']}/", {"name": "Y"}, format="json").status_code == 404
        assert c.patch(API + f"finding-categories/{r.json()['id']}/", {"name": "Hygiene & cleaning", "is_active": False}, format="json").json()["is_active"] is False
        assert c.patch(API + f"finding-categories/{r.json()['id']}/", {"code": "renamed"}, format="json").status_code == 400
        assert c.delete(API + f"finding-categories/{r.json()['id']}/").status_code == 405

    def test_severity_management_and_validation(self, w):
        c = api(w["admin"])
        r = c.post(API + "finding-severities/", {"code": "blocker", "name": "Blocker", "rank": 50, "is_escalation": True}, format="json")
        assert r.status_code == 201 and r.json()["rank"] == 50
        assert c.post(API + "finding-severities/", {"code": "dup", "name": "Dup", "rank": 50}, format="json").status_code == 409
        assert c.post(API + "finding-severities/", {"code": "bad", "name": "Bad", "rank": 0}, format="json").status_code == 400
        assert c.post(API + "finding-severities/", {"code": "bad2", "name": "Bad", "rank": 5, "is_system": True}, format="json").status_code == 400
        assert c.patch(API + f"finding-severities/{r.json()['id']}/", {"rank": 60, "requires_owner": True}, format="json").json()["requires_owner"] is True
        assert api(w["editor"]).post(API + "finding-severities/", {"code": "z", "name": "Z", "rank": 3}, format="json").status_code == 403
