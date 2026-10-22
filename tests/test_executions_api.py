"""``/api/v1/executions/``: contract, answers, validation, submission, scope isolation, protected fields."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.core.models import ActivityLog
from apps.executions import services
from apps.executions.models import ChecklistExecution, ChecklistResponse

from executions_support import REQUIRED, build_exec_world, issued, item_id

BASE = "/api/v1/executions/"


class Client(APIClient):
    """Unwraps the standard ``{"data": ..., "meta": ...}`` success envelope so assertions read the payload."""

    def request(self, **kw):  # noqa: ANN003, ANN201
        response = super().request(**kw)
        original = response.json

        def unwrapped():  # noqa: ANN202
            body = original()
            if isinstance(body, dict) and "data" in body and "error" not in body:
                if "meta" in body and isinstance(body["data"], list):
                    return {"results": body["data"], **body["meta"]}
                return body["data"]
            return body

        response.json = unwrapped
        return response


def api(user) -> Client:
    client = Client()
    if user is not None:
        client.force_authenticate(user)
    return client


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_exec_world(org_a, org_b, make_user, assign, make_type)


def started(w, who="runner", **kw):
    ex, _ = services.start_or_resume(issued(w, **kw), actor=w[who])
    return ex


def put(client, w, ex, code, value=None, **kw):
    return client.put(f"{BASE}{ex.pk}/responses/{item_id(w['eversion'], code)}/", {"value": value, **kw}, format="json")


@pytest.mark.django_db
class TestStartAndRead:
    def test_anonymous_is_rejected(self, w):
        assert api(None).get(BASE).status_code in (401, 403)
        assert api(None).get("/api/v1/my-assignments/").status_code in (401, 403)

    def test_my_assignments_and_start_are_idempotent(self, w):
        a, foreign = issued(w, w["r1"]), issued(w, w["r3"])
        client = api(w["runner"])
        rows = client.get("/api/v1/my-assignments/").json()["results"]
        assert [r["id"] for r in rows] == [str(a.pk)] and rows[0]["execution"] is None
        first = client.post(f"/api/v1/assignments/{a.pk}/start-execution/")
        again = client.post(f"/api/v1/assignments/{a.pk}/start-execution/")
        assert (first.status_code, again.status_code) == (201, 200) and first.json()["id"] == again.json()["id"]
        assert first.json()["status"] == "in_progress" and first.json()["checklist"]["version"] == 1
        assert client.post(f"/api/v1/assignments/{foreign.pk}/start-execution/").status_code == 404
        assert client.post(f"/api/v1/assignments/{a.pk}/start-execution/", {"status": "submitted"}, format="json").status_code == 400
        assert client.get("/api/v1/my-assignments/").json()["results"][0]["execution"]["status"] == "in_progress"
        assert ChecklistExecution.objects.count() == 1

    def test_assignment_execution_lookup(self, w):
        ex = started(w)
        assert api(w["runner"]).get(f"/api/v1/assignments/{ex.assignment_id}/execution/").json()["id"] == str(ex.pk)
        assert api(w["other"]).get(f"/api/v1/assignments/{ex.assignment_id}/execution/").status_code == 404
        assert api(w["runner"]).get(f"/api/v1/assignments/{issued(w, w['r2']).pk}/execution/").status_code == 404

    def test_list_retrieve_filters(self, w):
        ex = started(w)
        client = api(w["runner"])
        row = client.get(BASE).json()["results"][0]
        assert row["reference"] == ex.reference and row["location"]["path"] == "Region North / Area 1 / Restaurant 1"
        assert client.get(f"{BASE}{ex.pk}/").json()["id"] == str(ex.pk)
        assert len(client.get(BASE, {"status": "submitted"}).json()["results"]) == 0
        assert len(client.get(BASE, {"status": "in_progress", "version": 1}).json()["results"]) == 1

    def test_other_org_and_scope_are_404_everywhere(self, w):
        ex = started(w)
        for who in ("other", "plain"):
            c = api(w[who])
            assert c.get(f"{BASE}{ex.pk}/").status_code == 404, who
            assert c.get(f"{BASE}{ex.pk}/progress/").status_code == 404
            assert c.get(f"{BASE}{ex.pk}/responses/").status_code == 404
            assert c.get(f"{BASE}{ex.pk}/history/").status_code == 404
            assert put(c, w, ex, "FIRE-EXIT", "yes").status_code == 404
            assert c.post(f"{BASE}{ex.pk}/submit/").status_code == 404
            assert c.post(f"{BASE}{ex.pk}/validate/").status_code == 404
        assert api(w["other"]).get(BASE).json()["results"] == []
        assert not ex.responses.exists()

    def test_no_delete_no_patch_no_create(self, w):
        ex = started(w)
        c = api(w["admin"])
        assert c.delete(f"{BASE}{ex.pk}/").status_code == 405
        assert c.patch(f"{BASE}{ex.pk}/", {"status": "submitted"}, format="json").status_code == 405
        assert c.post(BASE, {"assignment": str(ex.assignment_id)}, format="json").status_code == 405


@pytest.mark.django_db
class TestAnswers:
    def test_put_a_response_then_read_it_back(self, w):
        ex = started(w)
        c = api(w["runner"])
        r = put(c, w, ex, "FRIDGE", "4.5", comment="ok")
        assert r.status_code == 200 and r.json()["saved"] == [item_id(w["eversion"], "FRIDGE")] and r.json()["progress"]["answered"] == 1
        rows = c.get(f"{BASE}{ex.pk}/responses/").json()["results"]
        assert rows[0]["code"] == "FRIDGE" and rows[0]["value"] == "4.5" and rows[0]["revision"] == 1 and rows[0]["comment"] == "ok"
        assert c.get(f"{BASE}{ex.pk}/progress/").json()["percent"] == 33

    def test_invalid_values_are_400_with_structured_errors(self, w):
        ex = started(w)
        r = put(api(w["runner"]), w, ex, "FRIDGE", "99")
        body = r.json()["error"]["details"]
        assert r.status_code == 400 and body["errors"][item_id(w["eversion"], "FRIDGE")][0]["code"] == "out_of_range" and body["code"] == "validation_failed"
        assert not ex.responses.exists()

    def test_stale_revisions_are_409(self, w):
        ex = started(w)
        c = api(w["runner"])
        put(c, w, ex, "FRIDGE", "4")
        stale = put(c, w, ex, "FRIDGE", "5", revision=0)
        assert stale.status_code == 409 and stale.json()["error"]["details"]["conflicts"] == {item_id(w["eversion"], "FRIDGE"): 1}
        assert put(c, w, ex, "FRIDGE", "5", revision=1).status_code == 200

    def test_items_outside_the_version_or_hidden_are_refused(self, w):
        from apps.checklists.models import ChecklistItem

        ex = started(w)
        c = api(w["runner"])
        foreign = str(ChecklistItem.objects.filter(section__version=w["version"]).first().pk)
        r = c.put(f"{BASE}{ex.pk}/responses/{foreign}/", {"value": "yes"}, format="json")
        assert r.status_code == 400 and r.json()["error"]["details"]["errors"][foreign][0]["code"] == "unknown_item"
        assert put(c, w, ex, "ISSUE", "x").json()["error"]["details"]["errors"][item_id(w["eversion"], "ISSUE")][0]["code"] == "item_hidden"
        assert c.put(f"{BASE}{ex.pk}/responses/not-a-uuid/", {"value": "yes"}, format="json").status_code == 400
        assert not ex.responses.exists()

    def test_media_answers_are_refused_until_evidence_exists(self, w):
        ex = started(w)
        r = put(api(w["runner"]), w, ex, "SIGN", {"signer_name": "A", "file_id": None})
        assert r.status_code == 400 and r.json()["error"]["details"]["errors"][item_id(w["eversion"], "SIGN")][0]["code"] == "use_evidence_upload"

    def test_protected_fields_are_rejected(self, w, org_b):
        ex = started(w)
        c = api(w["runner"])
        for field, value in (("status", "submitted"), ("organization", str(org_b.pk)), ("assignment", str(ex.assignment_id)), ("item", str(ex.pk)),
                             ("submitted_by", str(w["runner"].pk)), ("nonsense", 1)):
            r = c.put(f"{BASE}{ex.pk}/responses/{item_id(w['eversion'], 'FRIDGE')}/", {"value": "4", field: value}, format="json")
            assert r.status_code == 400, field
        assert not ex.responses.exists()

    def test_save_draft_batch_is_partial(self, w):
        ex = started(w)
        c = api(w["runner"])
        body = {"responses": [{"item": item_id(w["eversion"], "FIRE-EXIT"), "value": "yes"}, {"item": item_id(w["eversion"], "FRIDGE"), "value": "99"},
                              {"item": item_id(w["eversion"], "HANDWASH"), "value": "yes"}]}
        r = c.post(f"{BASE}{ex.pk}/save-draft/", body, format="json")
        assert r.status_code == 400 and len(r.json()["error"]["details"]["saved"]) == 2
        assert ex.responses.count() == 2
        assert c.post(f"{BASE}{ex.pk}/save-draft/", {"responses": []}, format="json").status_code == 400
        assert c.post(f"{BASE}{ex.pk}/save-draft/", {"responses": [{"item": item_id(w["eversion"], "BOOL"), "value": True, "status": "x"}]}, format="json").status_code == 400

    def test_permissions(self, w):
        ex = started(w)
        r = put(api(w["manager"]), w, ex, "FIRE-EXIT", "yes")  # may view (execution.view at Area 1), may not update
        assert r.status_code == 403 and not ex.responses.exists()


@pytest.mark.django_db
class TestSubmission:
    def fill(self, c, w, ex):
        return c.post(f"{BASE}{ex.pk}/save-draft/", {"responses": [{"item": item_id(w["eversion"], k), "value": v} for k, v in REQUIRED.items()]}, format="json")

    def test_validate_is_a_dry_run(self, w):
        ex = started(w)
        c = api(w["runner"])
        r = c.post(f"{BASE}{ex.pk}/validate/")
        assert r.status_code == 200 and r.json()["valid"] is False and len(r.json()["problems"]) == 3 and r.json()["problems"][0]["problems"][0]["code"] == "required"
        self.fill(c, w, ex)
        assert c.post(f"{BASE}{ex.pk}/validate/").json()["valid"] is True
        assert ChecklistExecution.objects.get(pk=ex.pk).status == "in_progress"

    def test_invalid_submission_is_400_and_changes_nothing(self, w):
        ex = started(w)
        c = api(w["runner"])
        r = c.post(f"{BASE}{ex.pk}/submit/")
        assert r.status_code == 400 and r.json()["error"]["details"]["code"] == "validation_failed" and len(r.json()["error"]["details"]["problems"]) == 3
        assert ChecklistExecution.objects.get(pk=ex.pk).status == "in_progress"

    def test_submit_is_idempotent_and_final(self, w):
        ex = started(w)
        c = api(w["runner"])
        self.fill(c, w, ex)
        first = c.post(f"{BASE}{ex.pk}/submit/")
        second = c.post(f"{BASE}{ex.pk}/submit/")
        assert first.status_code == second.status_code == 200
        assert (first.json()["already_submitted"], second.json()["already_submitted"]) == (False, True) and first.json()["status"] == "submitted"
        assert ActivityLog.objects.filter(verb="execution.submitted").count() == 1
        edit = put(c, w, ex, "FIRE-EXIT", "no", revision=1)
        assert edit.status_code == 409 and edit.json()["error"]["details"]["code"] == "already_submitted"
        assert ChecklistResponse.objects.get(execution=ex, item__code="FIRE-EXIT").value == "yes"
        assert c.post(f"{BASE}{ex.pk}/cancel/", {"reason": "x"}, format="json").status_code in (403, 409)

    def test_assignment_completes_only_through_submission(self, w):
        ex = started(w)
        c = api(w["runner"])
        self.fill(c, w, ex)
        ex.assignment.refresh_from_db()
        assert ex.assignment.status == "in_progress"
        c.post(f"{BASE}{ex.pk}/submit/")
        ex.assignment.refresh_from_db()
        assert ex.assignment.status == "completed"

    def test_submit_needs_permission(self, w):
        ex = started(w)
        self.fill(api(w["runner"]), w, ex)
        assert api(w["manager"]).post(f"{BASE}{ex.pk}/submit/").status_code == 403
        assert ChecklistExecution.objects.get(pk=ex.pk).status == "in_progress"

    def test_cancel_and_history(self, w):
        ex = started(w)
        assert api(w["runner"]).post(f"{BASE}{ex.pk}/cancel/", {"reason": "x"}, format="json").status_code == 403
        assert api(w["admin"]).post(f"{BASE}{ex.pk}/cancel/", {}, format="json").status_code == 400
        r = api(w["admin"]).post(f"{BASE}{ex.pk}/cancel/", {"reason": "Closed"}, format="json")
        assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["cancel_reason"] == "Closed"
        h = api(w["runner"]).get(f"{BASE}{ex.pk}/history/").json()["results"]
        assert {e["verb"] for e in h} >= {"execution.started", "execution.cancelled"}
        assert put(api(w["runner"]), w, ex, "FIRE-EXIT", "yes").status_code == 409


@pytest.mark.django_db
def test_list_query_count_is_flat(w, django_assert_max_num_queries):
    started(w)
    c = api(w["area_runner"])
    c.get(BASE)
    started(w, "area_runner", location=w["r2"])
    with django_assert_max_num_queries(25):
        assert c.get(BASE).status_code == 200

