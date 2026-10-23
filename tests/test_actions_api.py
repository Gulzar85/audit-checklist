"""Phase 13 REST API: list/filter/sort, creation (also nested under a finding), planning edits, lifecycle endpoints, work log, evidence,
worklists, verification queue, summary and isolation."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.corrective_actions import services
from apps.corrective_actions.models import CorrectiveAction, CorrectiveActionVerification
from apps.evidence import lifecycle as evidence_lifecycle
from apps.evidence.scanning import ScanResult

from actions_support import build_actions_world, future, make_action, make_finding, to_status
from evidence_support import evidence_storage, jpeg_bytes  # noqa: F401
from test_evidence_web_api import api

API = "/api/v1/"


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_actions_world(org_a, org_b, make_user, assign, make_type)


def body(w, finding=None, **kw):
    return {"finding": str((finding or make_finding(w)).pk), "title": "Repair the seal", **kw}


class TestListAndFilters:
    def test_list_is_scoped_paginated_filterable_and_sortable(self, w):
        f = make_finding(w)
        a = make_action(w, f, title="Alpha", priority="low")
        b = make_action(w, f, title="Beta", priority="critical")
        assert {r["id"] for r in api(w["manager"]).get(API + "corrective-actions/").json()["results"]} == {str(a.pk), str(b.pk)}
        assert api(w["b_admin"]).get(API + "corrective-actions/").json()["results"] == []
        assert api(w["r2_actor"]).get(API + "corrective-actions/").json()["results"] == []
        assert [r["title"] for r in api(w["manager"]).get(API + "corrective-actions/", {"search": "Alp"}).json()["results"]] == ["Alpha"]
        assert [r["title"] for r in api(w["manager"]).get(API + "corrective-actions/", {"priority": "critical"}).json()["results"]] == ["Beta"]
        assert [r["title"] for r in api(w["manager"]).get(API + "corrective-actions/", {"ordering": "-priority_order"}).json()["results"]] == ["Beta", "Alpha"]
        assert [r["id"] for r in api(w["manager"]).get(API + "corrective-actions/", {"finding": str(f.pk)}).json()["results"]].__len__() == 2

    def test_the_overdue_filter_follows_the_derived_rule(self, w):
        late, fine, done = make_action(w, title="Late"), make_action(w, title="Fine", due_at=future(5)), make_action(w, title="Done")
        CorrectiveAction.objects.filter(pk__in=[late.pk, done.pk]).update(due_at=timezone.now() - timedelta(days=1))
        to_status(w, CorrectiveAction.objects.get(pk=done.pk), "submitted")  # awaiting verification is not "overdue"
        rows = api(w["manager"]).get(API + "corrective-actions/", {"status": "overdue"}).json()["results"]
        assert [r["title"] for r in rows] == ["Late"] and rows[0]["is_overdue"] and rows[0]["effective_status"] == "overdue" and rows[0]["status"] == "open"
        _ = fine

    def test_filters_cannot_name_things_outside_the_scope(self, w):
        make_action(w)
        foreign = make_finding(w, location=w["r2"], title="Elsewhere")
        assert api(w["aviewer"]).get(API + "corrective-actions/", {"finding": str(foreign.pk)}).status_code == 400

    def test_authentication_is_required(self, w):
        assert api(None).get(API + "corrective-actions/").status_code in (401, 403)


class TestCreate:
    def test_create_and_nested_create(self, w):
        f = make_finding(w)
        r = api(w["manager"]).post(API + "corrective-actions/", body(w, f, assigned_to=str(w["worker1"].pk), due_at=future().isoformat(), priority="high"), format="json")
        j = r.json()
        assert r.status_code == 201 and j["reference"] == "CA-00001" and j["status"] == "draft" and j["created_by"] == str(w["manager"].pk) and j["finding"]["id"] == str(f.pk)
        assert j["allowed_actions"] == ["issue", "cancel"] and j["finding_eligibility"]["eligible"] is False
        n = api(w["manager"]).post(API + f"findings/{f.pk}/corrective-actions/", {"title": "Update the procedure", "issue": False}, format="json")
        assert n.status_code == 201 and n.json()["finding"]["id"] == str(f.pk)
        listing = api(w["manager"]).get(API + f"findings/{f.pk}/corrective-actions/").json()
        assert len(listing["results"]) == 2 and listing["eligibility"]["eligible"] is False and sorted(listing["eligibility"]["outstanding"]) == ["CA-00001", "CA-00002"]

    def test_forged_fields_are_rejected_not_ignored(self, w):
        for field, value in (("organization", str(w["org_b"].pk)), ("status", "verified"), ("created_by", str(w["admin"].pk)), ("verified_by", str(w["verifier"].pk)),
                             ("number", 7), ("location", str(w["r2"].pk)), ("submitted_at", "2020-01-01T00:00:00Z"), ("independent_verification", False)):
            r = api(w["manager"]).post(API + "corrective-actions/", body(w, **{field: value}), format="json")
            assert r.status_code == 400, field
        assert not CorrectiveAction.objects.exists()

    def test_references_are_validated_server_side(self, w):
        r = api(w["manager"]).post(API + "corrective-actions/", {"finding": "00000000-0000-0000-0000-000000000000", "title": "x"}, format="json")
        assert r.status_code == 400
        foreign = make_finding(w, location=w["r2"], title="Elsewhere")
        assert api(w["manager"]).post(API + "corrective-actions/", body(w, foreign), format="json").status_code == 400  # not visible to the manager: "unknown"
        assert api(w["b_admin"]).post(API + "corrective-actions/", body(w), format="json").status_code == 400
        assert api(w["manager"]).post(API + "corrective-actions/", body(w, assigned_to=str(w["aviewer"].pk)), format="json").status_code == 400  # ineligible owner

    def test_permission_is_required(self, w):
        assert api(w["aviewer"]).post(API + "corrective-actions/", body(w), format="json").status_code == 403
        f = make_finding(w)
        assert api(w["viewer"]).post(API + f"findings/{f.pk}/corrective-actions/", {"title": "x"}, format="json").status_code == 403  # can see the finding only

    def test_a_closed_finding_takes_no_new_actions(self, w):
        from apps.findings import services as finding_services

        f = make_finding(w)
        finding_services.transition(f, "cancel", actor=w["admin"], reason="Error")
        r = api(w["manager"]).post(API + "corrective-actions/", body(w, f), format="json")
        assert r.status_code == 409 and r.json()["error"]["details"]["code"] == "finding_not_open"


class TestPatchAndAssign:
    def test_patch_changes_planning_fields_only(self, w):
        a = make_action(w)
        r = api(w["manager"]).patch(API + f"corrective-actions/{a.pk}/", {"title": "New", "priority": "high", "required_evidence": 2}, format="json")
        assert r.status_code == 200 and r.json()["title"] == "New" and r.json()["required_evidence"] == 2
        for field in ("status", "organization", "finding", "assigned_to", "verified_at", "reference", "submission_count"):
            assert api(w["manager"]).patch(API + f"corrective-actions/{a.pk}/", {field: "x"}, format="json").status_code == 400, field
        assert api(w["manager"]).put(API + f"corrective-actions/{a.pk}/", {}, format="json").status_code == 405
        assert api(w["manager"]).delete(API + f"corrective-actions/{a.pk}/").status_code == 405

    def test_the_owner_edits_the_plan_but_not_the_deadline(self, w):
        a = make_action(w)
        assert api(w["worker1"]).patch(API + f"corrective-actions/{a.pk}/", {"plan": "Order the seal"}, format="json").status_code == 200
        assert api(w["worker1"]).patch(API + f"corrective-actions/{a.pk}/", {"due_at": future(9).isoformat()}, format="json").status_code == 403
        assert api(w["worker2"]).patch(API + f"corrective-actions/{a.pk}/", {"plan": "x"}, format="json").status_code == 403  # sees it (same location) but is not the owner

    def test_assign_and_reassign(self, w):
        a = make_action(w, owner=None, issue=False)
        r = api(w["manager"]).post(API + f"corrective-actions/{a.pk}/assign/", {"assigned_to": str(w["worker1"].pk)}, format="json")
        assert r.status_code == 200 and r.json()["assigned_to"]["email"] == w["worker1"].email
        assert api(w["manager"]).post(API + f"corrective-actions/{a.pk}/reassign/", {"assigned_to": str(w["worker2"].pk)}, format="json").json()["assigned_to"]["email"] == w["worker2"].email
        assert api(w["manager"]).post(API + f"corrective-actions/{a.pk}/assign/", {"assigned_to": str(w["aviewer"].pk)}, format="json").status_code == 400
        assert api(w["worker2"]).post(API + f"corrective-actions/{a.pk}/reassign/", {"assigned_to": str(w["worker1"].pk)}, format="json").status_code == 403


class TestLifecycleEndpoints:
    def test_the_full_cycle(self, w):
        a = make_action(w, issue=False)
        m, o, v = api(w["manager"]), api(w["worker1"]), api(w["verifier"])
        assert m.post(API + f"corrective-actions/{a.pk}/issue/").json()["status"] == "open"
        assert o.post(API + f"corrective-actions/{a.pk}/start/").json()["status"] == "in_progress"
        assert o.post(API + f"corrective-actions/{a.pk}/block/", {"reason": "Waiting for a part"}, format="json").json()["status"] == "blocked"
        assert o.post(API + f"corrective-actions/{a.pk}/resume/").json()["status"] == "in_progress"
        r = o.post(API + f"corrective-actions/{a.pk}/submit-for-verification/", {"reason": "Seal replaced and tested."}, format="json")
        assert r.json()["status"] == "submitted" and r.json()["submission_count"] == 1
        assert v.post(API + f"corrective-actions/{a.pk}/reject/", {"reason": "Not tested under load"}, format="json").json()["status"] == "rejected"
        assert o.post(API + f"corrective-actions/{a.pk}/resume/").json()["status"] == "in_progress"
        o.post(API + f"corrective-actions/{a.pk}/submit-for-verification/", {"reason": "Retested."}, format="json")
        done = v.post(API + f"corrective-actions/{a.pk}/verify/", {"reason": "Checked on site"}, format="json").json()
        assert done["status"] == "verified" and done["verified_by"] == str(w["verifier"].pk) and [x["decision"] for x in done["verifications"]] == ["rejected", "approved"]
        assert m.post(API + f"corrective-actions/{a.pk}/close/").json()["status"] == "closed"
        assert m.post(API + f"corrective-actions/{a.pk}/reopen/", {"reason": "It leaks again"}, format="json").json()["status"] == "in_progress"

    def test_errors_use_the_right_status_codes(self, w):
        a = make_action(w)
        assert api(w["worker1"]).post(API + f"corrective-actions/{a.pk}/block/").status_code == 400  # wrong state (open) -> invalid_transition
        api(w["worker1"]).post(API + f"corrective-actions/{a.pk}/start/")
        assert api(w["worker1"]).post(API + f"corrective-actions/{a.pk}/start/").status_code == 409  # a repeat
        assert api(w["worker1"]).post(API + f"corrective-actions/{a.pk}/submit-for-verification/").status_code == 400  # a completion summary is required
        assert api(w["worker1"]).post(API + f"corrective-actions/{a.pk}/verify/").status_code == 403
        assert api(w["worker2"]).post(API + f"corrective-actions/{a.pk}/start/").status_code == 403  # a colleague, not the owner
        assert api(w["b_admin"]).post(API + f"corrective-actions/{a.pk}/start/").status_code == 404
        assert api(w["aviewer"]).post(API + f"corrective-actions/{a.pk}/start/").status_code == 403
        assert api(None).post(API + f"corrective-actions/{a.pk}/start/").status_code in (401, 403)

    def test_self_verification_is_refused(self, w):
        a = make_action(w, owner="both")
        api(w["both"]).post(API + f"corrective-actions/{a.pk}/start/")
        api(w["both"]).post(API + f"corrective-actions/{a.pk}/submit-for-verification/", {"reason": "Done"}, format="json")
        for name in ("verify", "reject"):
            assert api(w["both"]).post(API + f"corrective-actions/{a.pk}/{name}/", {"reason": "mine"}, format="json").status_code == 403
        assert not CorrectiveActionVerification.objects.exists()

    def test_status_and_decisions_cannot_be_written_by_any_payload(self, w):
        a = make_action(w)
        r = api(w["worker1"]).post(API + f"corrective-actions/{a.pk}/start/", {"status": "verified", "reason": "x"}, format="json")
        assert r.status_code == 400 and CorrectiveAction.objects.get(pk=a.pk).status == "open"
        r = api(w["verifier"]).post(API + f"corrective-actions/{a.pk}/verify/", {"decision": "approved", "verifier": str(w["admin"].pk)}, format="json")
        assert r.status_code == 400

    def test_the_evidence_requirement_is_enforced_on_submission(self, w):
        a = to_status(w, make_action(w, required_evidence=1), "in_progress")
        r = api(w["worker1"]).post(API + f"corrective-actions/{a.pk}/submit-for-verification/", {"reason": "Done"}, format="json")
        assert r.status_code == 400 and r.json()["error"]["details"]["code"] == "evidence_required"


class TestWorkLogAndHistory:
    def test_updates_list_and_append(self, w):
        a = to_status(w, make_action(w), "in_progress")
        r = api(w["worker1"]).post(API + f"corrective-actions/{a.pk}/updates/", {"body": "Seal ordered", "progress_percent": 40}, format="json")
        assert r.status_code == 201 and r.json()["kind"] == "progress" and r.json()["progress_percent"] == 40
        fix = api(w["worker1"]).post(API + f"corrective-actions/{a.pk}/updates/", {"body": "Ordered Monday", "corrects": r.json()["id"]}, format="json")
        assert fix.status_code == 201 and fix.json()["kind"] == "correction" and fix.json()["corrects"] == r.json()["id"]
        rows = api(w["aviewer"]).get(API + f"corrective-actions/{a.pk}/updates/").json()["results"]
        assert [x["kind"] for x in rows] == ["progress", "correction"]
        assert api(w["worker1"]).post(API + f"corrective-actions/{a.pk}/updates/", {"body": "x", "progress_percent": 150}, format="json").status_code == 400
        assert api(w["worker1"]).post(API + f"corrective-actions/{a.pk}/updates/", {"body": "x", "kind": "submission"}, format="json").status_code == 400
        assert api(w["aviewer"]).post(API + f"corrective-actions/{a.pk}/updates/", {"body": "x"}, format="json").status_code == 403
        assert api(w["b_admin"]).get(API + f"corrective-actions/{a.pk}/updates/").status_code == 404

    def test_history_is_the_audit_trail(self, w):
        a = to_status(w, make_action(w), "in_progress")
        rows = api(w["aviewer"]).get(API + f"corrective-actions/{a.pk}/history/").json()["results"]
        assert [r["verb"] for r in rows][:2] == ["corrective_action.started", "corrective_action.issued"] and rows[-1]["verb"] == "corrective_action.created"


class TestEvidenceApi:
    def upload(self, w, a, who="worker1", purpose="completion", **kw):
        return api(w[who]).post(API + f"corrective-actions/{a.pk}/evidence/", {"purpose": purpose, "file": SimpleUploadedFile("proof.jpg", jpeg_bytes()), **kw}, format="multipart")

    def test_upload_list_download(self, w):
        a = to_status(w, make_action(w), "in_progress")
        r = self.upload(w, a)
        assert r.status_code == 201 and r.json()["purpose"] == "completion" and r.json()["available"] is True
        rows = api(w["aviewer"]).get(API + f"corrective-actions/{a.pk}/evidence/").json()["results"]
        assert len(rows) == 1 and rows[0]["available"] and "storage" not in str(rows).lower()
        d = api(w["aviewer"]).get(API + f"corrective-actions/{a.pk}/evidence/{rows[0]['id']}/download/")
        assert d.status_code == 200 and d["X-Content-Type-Options"] == "nosniff" and d.content[:2] == b"\xff\xd8"
        assert api(w["aviewer"]).get(API + f"corrective-actions/{a.pk}/evidence/{rows[0]['id']}/preview/")["Content-Disposition"].startswith("inline")

    def test_purposes_follow_the_stage_and_the_actor(self, w):
        a = to_status(w, make_action(w), "in_progress")
        assert self.upload(w, a, purpose="verification", who="verifier").status_code in (403, 404, 409)  # not submitted yet
        assert self.upload(w, a, who="aviewer").status_code == 403
        assert self.upload(w, a, who="worker2").status_code == 403  # a colleague, not the owner
        assert self.upload(w, a, purpose="bogus").status_code == 400
        s = to_status(w, make_action(w, title="two"), "submitted")
        assert self.upload(w, s, who="worker1").status_code == 409  # submitted work is locked
        assert self.upload(w, s, purpose="verification", who="verifier").status_code == 201
        assert self.upload(w, s, purpose="rejection", who="verifier").status_code == 201

    def test_unknown_fields_and_idempotency(self, w):
        import uuid

        a = to_status(w, make_action(w), "in_progress")
        assert self.upload(w, a, organization=str(w["org_b"].pk)).status_code == 400
        op = str(uuid.uuid4())
        first, second = self.upload(w, a, operation_id=op), self.upload(w, a, operation_id=op)
        assert (first.status_code, second.status_code) == (201, 200) and second.json()["duplicate"] is True

    def test_quarantined_files_are_listed_but_never_served_or_counted(self, w):
        from apps.corrective_actions import selectors
        from apps.evidence.models import EvidenceAttachment

        a = to_status(w, make_action(w, required_evidence=1), "in_progress")
        att_id = self.upload(w, a).json()["id"]
        att = EvidenceAttachment.objects.get(pk=att_id)
        assert selectors.verified_completion_count(a) == 1
        evidence_lifecycle.apply_scan(att.asset, ScanResult("infected", "EICAR"))
        assert selectors.verified_completion_count(a) == 0
        row = api(w["aviewer"]).get(API + f"corrective-actions/{a.pk}/evidence/").json()["results"][0]
        assert row["status"] == "quarantined" and row["download_url"] is None
        assert api(w["aviewer"]).get(API + f"corrective-actions/{a.pk}/evidence/{att_id}/download/").status_code == 404
        r = api(w["worker1"]).post(API + f"corrective-actions/{a.pk}/submit-for-verification/", {"reason": "Done"}, format="json")
        assert r.status_code == 400 and r.json()["error"]["details"]["code"] == "evidence_required"

    def test_another_actions_file_is_unreachable(self, w):
        a = to_status(w, make_action(w), "in_progress")
        b = make_action(w, title="Other")
        att_id = self.upload(w, a).json()["id"]
        assert api(w["manager"]).get(API + f"corrective-actions/{b.pk}/evidence/{att_id}/download/").status_code == 404

    def test_the_generic_attachment_endpoints_follow_the_same_authorization(self, w):
        a = to_status(w, make_action(w), "in_progress")
        att_id = self.upload(w, a).json()["id"]
        assert api(w["aviewer"]).get(API + f"attachments/{att_id}/").status_code == 200
        assert api(w["b_admin"]).get(API + f"attachments/{att_id}/").status_code == 404
        assert api(w["r2_actor"]).get(API + f"attachments/{att_id}/").status_code == 404


class TestWorklistsAndSummary:
    def test_my_actions(self, w):
        mine, theirs, draft = make_action(w, title="Mine"), make_action(w, title="Theirs", owner="worker2"), make_action(w, title="Draft", issue=False)
        rows = api(w["worker1"]).get(API + "my-corrective-actions/").json()["results"]
        assert [r["title"] for r in rows] == ["Mine"]  # drafts are not theirs yet; other people's actions are never listed
        assert [r["title"] for r in api(w["worker1"]).get(API + "corrective-actions/mine/").json()["results"]] == ["Mine"]
        _ = (theirs, draft, mine)

    def test_the_verification_queue_is_scoped_and_independent(self, w):
        s = to_status(w, make_action(w, owner="both"), "submitted", owner="both")
        t = to_status(w, make_action(w, title="Another"), "submitted")
        assert {r["id"] for r in api(w["verifier"]).get(API + "corrective-actions/verification-queue/").json()["results"]} == {str(s.pk), str(t.pk)}
        assert [r["id"] for r in api(w["both"]).get(API + "corrective-actions/verification-queue/").json()["results"]] == [str(t.pk)]  # never their own work
        assert api(w["worker1"]).get(API + "corrective-actions/verification-queue/").status_code == 403
        assert api(w["r2_actor"]).get(API + "corrective-actions/verification-queue/").json()["results"] == []  # verify permission, wrong location
        assert api(w["b_admin"]).get(API + "corrective-actions/verification-queue/").json()["results"] == []

    def test_summary_is_scoped(self, w):
        to_status(w, make_action(w, title="One"), "in_progress")
        to_status(w, make_action(w, title="Two"), "submitted")
        j = api(w["manager"]).get(API + "corrective-actions/summary/").json()
        assert (j["in_progress"], j["awaiting_verification"], j["open"]) == (1, 1, 2)
        assert api(w["b_admin"]).get(API + "corrective-actions/summary/").json()["total"] == 0
