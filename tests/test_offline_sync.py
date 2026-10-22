"""The offline protocol on the server: package, manifest, synchronization (idempotency, ordering, conflicts), conflict resolution."""

from __future__ import annotations

import copy
import uuid

import pytest
from rest_framework.test import APIClient

from apps.assignments import services as assignment_services
from apps.core.models import ActivityLog
from apps.executions import services
from apps.executions.models import ChecklistExecution, ChecklistResponse, SyncReceipt
from apps.executions.offline import compat

from executions_support import issued
from offline_support import build_offline_world, oid, oissued, op

BASE = "/api/v1/offline/"


class Client(APIClient):
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
    c = Client()
    if user is not None:
        c.force_authenticate(user)
    return c


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_offline_world(org_a, org_b, make_user, assign, make_type)


def sync(client, *ops, **extra):
    return client.post(BASE + "sync/", {"schema_version": 1, "operations": list(ops), **extra}, format="json")


def results(response):
    return response.json()["results"]


@pytest.mark.django_db
class TestManifestAndPackage:
    def test_manifest(self, w):
        data = api(w["runner"]).get(BASE + "manifest/").json()
        assert data["schema_version"] == 1 and "numeric" in data["supported_answer_types"] and data["unsupported_answer_types"] == ["photo", "file", "signature"]
        assert data["limits"]["max_operations"] == compat.MAX_OPERATIONS and data["evidence"] == "unavailable"
        assert api(None).get(BASE + "manifest/").status_code in (401, 403)

    def test_the_assignment_list_says_what_can_run_offline(self, w):
        ok, unsupported = oissued(w), issued(w, w["r2"])
        rows = {r["id"]: r for r in api(w["area_runner"]).get(BASE + "assignments/").json()["results"]}
        assert rows[str(ok.pk)]["offline"] == {"capable": True, "reasons": []}
        reasons = rows[str(unsupported.pk)]["offline"]["reasons"]
        assert rows[str(unsupported.pk)]["offline"]["capable"] is False and any("not available offline" in r for r in reasons)
        assert api(w["other"]).get(BASE + "assignments/").json()["results"] == []

    def test_the_package_is_the_exact_pinned_definition(self, w):
        a = oissued(w)
        r = api(w["runner"]).get(f"{BASE}assignments/{a.pk}/package/")
        p = r.json()
        assert r.status_code == 200 and r["Cache-Control"] == "no-store"
        assert p["checklist"]["version_id"] == str(w["oversion"].pk) and p["assignment"]["id"] == str(a.pk) and p["execution"] is None
        assert [i["code"] for i in p["items"]] == ["FIRE-EXIT", "HANDWASH", "FRIDGE", "TAGS", "PHOTO", "ISSUE"]
        assert p["rules"][0]["actions"][0]["type"] == "SHOW_ITEM" and p["package_hash"] and p["items"][2]["config"]["max"] == 10
        assert p["assignment"]["location"]["path"] == "Region North / Area 1 / Restaurant 1"
        assert {"password", "token", "permissions", "email"}.isdisjoint(str(p).lower().split())

    def test_the_package_carries_saved_answers_and_revisions(self, w):
        a = oissued(w)
        ex, _ = services.start_or_resume(a, actor=w["runner"])
        services.save_answers(ex, actor=w["runner"], inputs=[services.AnswerInput(oid(w, "FRIDGE"), "4")])
        p = api(w["runner"]).get(f"{BASE}assignments/{a.pk}/package/").json()
        assert p["execution"]["status"] == "in_progress" and p["execution"]["responses"][0]["revision"] == 1

    def test_unsupported_checklists_are_refused_with_reasons(self, w):
        a = issued(w)  # the Phase 9 checklist uses rules the offline evaluator does not implement
        r = api(w["runner"]).get(f"{BASE}assignments/{a.pk}/package/")
        assert r.status_code == 409 and r.json()["error"]["details"]["code"] == "offline_unsupported" and r.json()["error"]["details"]["reasons"]

    def test_a_package_is_404_outside_scope_or_organization(self, w):
        a = oissued(w, w["r3"])
        assert api(w["runner"]).get(f"{BASE}assignments/{a.pk}/package/").status_code == 404
        assert api(w["other"]).get(f"{BASE}assignments/{a.pk}/package/").status_code == 404
        assert api(w["plain"]).get(f"{BASE}assignments/{a.pk}/package/").status_code == 404
        assert api(None).get(f"{BASE}assignments/{a.pk}/package/").status_code in (401, 403)

    def test_a_newer_version_does_not_change_a_prepared_package(self, w):
        from apps.checklists import lifecycle

        a = oissued(w)
        before = api(w["runner"]).get(f"{BASE}assignments/{a.pk}/package/").json()
        draft = lifecycle.create_draft(w["otemplate"], change_summary="v2")
        lifecycle.publish_version(draft, expected_hash=lifecycle.run_validation(draft).draft_hash)
        after = api(w["runner"]).get(f"{BASE}assignments/{a.pk}/package/").json()
        assert before["package_hash"] == after["package_hash"] and after["checklist"]["version_number"] == 1


@pytest.mark.django_db
class TestSync:
    def test_answers_are_applied_with_receipts_and_audit(self, w):
        a = oissued(w)
        c = api(w["runner"])
        r = sync(c, op(w, a, "FIRE-EXIT", "yes", seq=1), op(w, a, "FRIDGE", "4.5", seq=2, comment="ok"))
        out = results(r)
        assert r.status_code == 200 and [x["status"] for x in out] == ["applied", "applied"] and out[0]["response_revision"] == 1
        ex = ChecklistExecution.objects.get()
        assert ex.responses.count() == 2 and SyncReceipt.objects.count() == 2 and ex.assignment.status == "in_progress"
        assert ActivityLog.objects.filter(verb="execution.offline_sync").count() == 1

    def test_retrying_an_operation_never_duplicates_it(self, w):
        a = oissued(w)
        c = api(w["runner"])
        first = op(w, a, "FRIDGE", "4.5")
        r1, r2, r3 = (results(sync(c, first))[0] for _ in range(3))
        assert r1["status"] == "applied" and not r1.get("duplicate") and r2["duplicate"] and r3["duplicate"]
        assert r2["response_revision"] == r1["response_revision"] == 1
        assert ChecklistResponse.objects.count() == 1 and SyncReceipt.objects.count() == 1
        assert ChecklistExecution.objects.get().revision == 2  # only one write happened

    def test_a_lost_response_is_recovered_by_a_retry_after_a_later_edit(self, w):
        a = oissued(w)
        c = api(w["runner"])
        first = op(w, a, "FRIDGE", "4.5")
        sync(c, first)  # the response "never arrived"
        second = op(w, a, "FRIDGE", "6", base=1)
        batch = results(sync(c, first, second))  # the retry of the first arrives together with the next edit
        assert batch[0]["duplicate"] and batch[1]["status"] == "applied"
        again = results(sync(c, first))[0]
        assert again["duplicate"] and ChecklistResponse.objects.get().value in ("6.0", "6")

    def test_an_operation_id_cannot_be_reused_with_a_different_payload(self, w):
        a = oissued(w)
        c = api(w["runner"])
        original = op(w, a, "FRIDGE", "4.5")
        sync(c, original)
        forged = {**original, "value": "9"}
        r = results(sync(c, forged))[0]
        assert r["status"] == "rejected" and r["code"] == "operation_id_reused"
        assert str(ChecklistResponse.objects.get().value).startswith("4.5")

    def test_operation_ids_are_scoped_to_the_user(self, w):
        a = oissued(w)
        shared = op(w, a, "FRIDGE", "4.5")
        sync(api(w["runner"]), shared)
        out = results(sync(api(w["runner2"]), shared))[0]  # a different user with the same id is a *new* operation (same execution)
        assert not out.get("duplicate")

    def test_operations_run_in_seq_order_whatever_the_array_order(self, w):
        a = oissued(w)
        c = api(w["runner"])
        late = op(w, a, "FRIDGE", "8", seq=2, base=1)
        early = op(w, a, "FRIDGE", "4", seq=1)
        out = results(sync(c, late, early))
        assert [x["status"] for x in out] == ["applied", "applied"] and str(ChecklistResponse.objects.get().value).startswith("8")
        assert ChecklistResponse.objects.get().revision == 2

    def test_invalid_answers_are_rejected_without_blocking_the_others(self, w):
        a = oissued(w)
        out = results(sync(api(w["runner"]), op(w, a, "FRIDGE", "99", seq=1), op(w, a, "FIRE-EXIT", "yes", seq=2)))
        assert out[0]["status"] == "rejected" and out[0]["code"] == "validation_failed" and out[0]["errors"][0]["code"] == "out_of_range"
        assert out[1]["status"] == "applied" and ChecklistResponse.objects.count() == 1

    def test_a_stale_revision_is_a_conflict_with_the_server_state(self, w):
        a = oissued(w)
        c = api(w["runner"])
        sync(c, op(w, a, "FRIDGE", "4"))
        server_edit = op(w, a, "FRIDGE", "5", base=1)
        sync(api(w["runner2"]), server_edit)  # another device moved it to revision 2
        out = results(sync(c, op(w, a, "FRIDGE", "9", base=1)))[0]
        assert out["status"] == "conflict" and out["code"] == "revision_mismatch"
        assert out["server"]["revision"] == 2 and str(out["server"]["value"]).startswith("5") and out["server"]["exists"] is True
        assert str(ChecklistResponse.objects.get().value).startswith("5")

    def test_submit_is_blocked_when_a_prerequisite_failed(self, w):
        a = oissued(w)
        c = api(w["runner"])
        out = results(sync(c, op(w, a, "FIRE-EXIT", "yes", seq=1), op(w, a, "HANDWASH", "yes", seq=2), op(w, a, "FRIDGE", "99", seq=3), op(w, a, type="submit", seq=4)))
        assert [x["status"] for x in out] == ["applied", "applied", "rejected", "blocked"] and out[3]["code"] == "dependency_failed"
        assert ChecklistExecution.objects.get().status == "in_progress" and not SyncReceipt.objects.filter(op_type="submit").exists()

    def test_submit_after_all_answers_succeeds_and_is_idempotent(self, w):
        a = oissued(w)
        c = api(w["runner"])
        submit = op(w, a, type="submit", seq=4)
        out = results(sync(c, op(w, a, "FIRE-EXIT", "yes", seq=1), op(w, a, "HANDWASH", "yes", seq=2), op(w, a, "FRIDGE", "4", seq=3), submit))
        assert [x["status"] for x in out] == ["applied"] * 4 and out[3]["execution_status"] == "submitted"
        a.refresh_from_db()
        assert a.status == "completed" and ActivityLog.objects.filter(verb="execution.submitted").count() == 1
        again = results(sync(c, submit))[0]
        assert again["duplicate"] and ActivityLog.objects.filter(verb="execution.submitted").count() == 1
        other_submit = results(sync(c, op(w, a, type="submit")))[0]  # a new id on an already submitted execution
        assert other_submit["status"] == "applied" and other_submit["already_submitted"] is True
        assert ActivityLog.objects.filter(verb="execution.submitted").count() == 1

    def test_submit_with_missing_answers_is_rejected_with_problems(self, w):
        a = oissued(w)
        out = results(sync(api(w["runner"]), op(w, a, "FIRE-EXIT", "yes", seq=1), op(w, a, type="submit", seq=2)))[1]
        assert out["status"] == "rejected" and out["code"] == "validation_failed" and len(out["problems"]) == 2
        assert ChecklistExecution.objects.get().status == "in_progress"

    def test_a_submitted_execution_cannot_be_changed_by_an_offline_client(self, w):
        a = oissued(w)
        ex, _ = services.start_or_resume(a, actor=w["runner"])
        services.save_answers(ex, actor=w["runner"], inputs=[services.AnswerInput(oid(w, c), v) for c, v in (("FIRE-EXIT", "yes"), ("HANDWASH", "yes"), ("FRIDGE", "4"))])
        services.submit(ex, actor=w["runner"])
        out = results(sync(api(w["runner"]), op(w, a, "FIRE-EXIT", "no", base=1)))[0]
        assert out["status"] == "conflict" and out["code"] == "execution_submitted"
        assert ChecklistResponse.objects.get(item__code="FIRE-EXIT").value == "yes"

    def test_a_cancelled_assignment_is_a_conflict_and_changes_nothing(self, w):
        a = oissued(w)
        sync(api(w["runner"]), op(w, a, "FRIDGE", "4"))
        assignment_services.cancel(a, actor=w["admin"], reason="Closed")
        out = results(sync(api(w["runner"]), op(w, a, "FRIDGE", "5", base=1)))[0]
        assert out["status"] in ("conflict", "rejected") and out["code"] in ("assignment_cancelled", "unavailable", "execution_cancelled")
        assert str(ChecklistResponse.objects.get().value).startswith("4")

    def test_stale_versions_are_rejected(self, w):
        a = oissued(w)
        stale = op(w, a, "FRIDGE", "4", version_id=str(w["eversion"].pk))
        assert results(sync(api(w["runner"]), stale))[0]["code"] == "stale_version"
        assert not ChecklistResponse.objects.exists()

    def test_hidden_and_foreign_items_are_rejected(self, w):
        from apps.checklists.models import ChecklistItem

        a = oissued(w)
        hidden = results(sync(api(w["runner"]), op(w, a, "ISSUE", "forged")))[0]
        assert hidden["status"] == "rejected" and hidden["errors"][0]["code"] == "item_hidden"
        foreign = op(w, a, "FRIDGE", "4", item_id=str(ChecklistItem.objects.filter(section__version=w["version"]).first().pk))
        assert results(sync(api(w["runner"]), foreign))[0]["errors"][0]["code"] == "unknown_item"

    def test_media_answers_are_rejected_offline_too(self, w):
        a = oissued(w)
        out = results(sync(api(w["runner"]), op(w, a, "PHOTO", [{"file_id": str(uuid.uuid4()), "name": "a.jpg", "size": 1, "content_type": "image/jpeg"}])))[0]
        assert out["status"] == "rejected" and out["errors"][0]["code"] == "evidence_unavailable"

    def test_clear_response(self, w):
        a = oissued(w)
        c = api(w["runner"])
        sync(c, op(w, a, "FRIDGE", "4"))
        out = results(sync(c, op(w, a, "FRIDGE", type="clear_response", base=1)))[0]
        assert out["status"] == "applied" and not ChecklistResponse.objects.exists()

    def test_client_timestamps_have_no_authority(self, w):
        a = oissued(w)
        c = api(w["runner"])
        out = results(sync(c, op(w, a, type="submit", client_created_at="1999-01-01T00:00:00Z", seq=-5)))[0]
        assert out["status"] == "rejected"  # validation, not the (forged) timestamp, decides
        sync(c, op(w, a, "FIRE-EXIT", "yes", client_created_at="2099-01-01T00:00:00Z"))
        row = ChecklistResponse.objects.get()
        assert row.answered_at.year == ChecklistExecution.objects.get().started_at.year and row.answered_at.year < 2099


@pytest.mark.django_db
class TestSyncSecurity:
    def test_anonymous_and_unprivileged(self, w):
        a = oissued(w)
        assert sync(api(None), op(w, a, "FRIDGE", "4")).status_code in (401, 403)
        out = results(sync(api(w["plain"]), op(w, a, "FRIDGE", "4")))[0]
        assert out["status"] == "rejected" and out["code"] == "unavailable"
        assert not ChecklistExecution.objects.exists()

    def test_cross_organization_and_scope_are_the_same_generic_answer(self, w):
        in_other_scope, mine = oissued(w, w["r3"]), oissued(w)
        for who, asg in (("other", mine), ("runner", in_other_scope)):
            out = results(sync(api(w[who]), op(w, asg, "FRIDGE", "4")))[0]
            assert out["status"] == "rejected" and out["code"] == "unavailable", who
        assert not ChecklistExecution.objects.exists() and not SyncReceipt.objects.exists()

    def test_a_designated_assignment_cannot_be_synced_by_someone_else(self, w):
        a = oissued(w, assigned_to=w["runner"])
        out = results(sync(api(w["runner2"]), op(w, a, "FRIDGE", "4")))[0]
        assert out["status"] == "rejected" and out["code"] == "unavailable"

    def test_revoked_permission_is_honoured_and_local_data_is_not_lost_server_side(self, w):
        a = oissued(w)
        sync(api(w["runner"]), op(w, a, "FRIDGE", "4"))
        from apps.permissions.models import MembershipRole

        MembershipRole.objects.filter(membership__user=w["runner"]).delete()
        from apps.permissions.authorization import invalidate_all

        invalidate_all()
        out = results(sync(api(w["runner"]), op(w, a, "FRIDGE", "5", base=1)))[0]
        assert out["status"] == "rejected" and out["code"] in ("forbidden", "unavailable")  # never applied, never confirmed
        assert str(ChecklistResponse.objects.get().value).startswith("4")

    def test_malformed_and_oversized_requests(self, w):
        a = oissued(w)
        c = api(w["runner"])
        assert c.post(BASE + "sync/", {"operations": "x"}, format="json").status_code == 400
        assert c.post(BASE + "sync/", {"operations": []}, format="json").status_code == 400
        assert c.post(BASE + "sync/", {"operations": [op(w, a, "FRIDGE", "4")], "evil": 1}, format="json").status_code == 400
        assert c.post(BASE + "sync/", {"schema_version": 99, "operations": [op(w, a, "FRIDGE", "4")]}, format="json").status_code == 400
        too_many = [op(w, a, "FIRE-EXIT", "yes", seq=i) for i in range(compat.MAX_OPERATIONS + 1)]
        assert c.post(BASE + "sync/", {"operations": too_many}, format="json").status_code == 400
        big = op(w, a, "ISSUE", "x" * (compat.MAX_VALUE_BYTES + 10))
        assert results(sync(c, big))[0]["code"] == "invalid_operation"
        assert not ChecklistExecution.objects.exists()

    def test_each_malformed_operation_is_rejected_individually(self, w):
        a = oissued(w)
        good = op(w, a, "FIRE-EXIT", "yes")
        bad = [{**op(w, a, "FRIDGE", "4"), "type": "drop_database"}, {**op(w, a, "FRIDGE", "4"), "status": "submitted"},
               {**op(w, a, "FRIDGE", "4"), "operation_id": "nope"}, {**op(w, a, "FRIDGE", "4"), "assignment_id": "x"}, "not an object",
               {**op(w, a, "FRIDGE", "4"), "base_revision": -1}, {**op(w, a, "FRIDGE", "4"), "schema_version": 7}]
        out = results(sync(api(w["runner"]), *bad, good))
        assert [x["status"] for x in out[:-1]] == ["rejected"] * 7 and all(x["code"] == "invalid_operation" for x in out[:-1])
        assert out[-1]["status"] == "applied" and ChecklistResponse.objects.count() == 1

    def test_sync_responses_are_never_cacheable(self, w):
        a = oissued(w)
        assert sync(api(w["runner"]), op(w, a, "FIRE-EXIT", "yes"))["Cache-Control"] == "no-store"


@pytest.mark.django_db(transaction=True)
class TestSyncConcurrency:
    def test_the_same_operation_sent_twice_at_once_is_applied_once(self, org_a, org_b, make_user, assign, make_type):
        import threading

        from django.db import connection

        w = build_offline_world(org_a, org_b, make_user, assign, make_type)
        a = oissued(w)
        shared = op(w, a, "FRIDGE", "4.5")
        out = []

        def run():
            try:
                out.append(results(sync(api(w["runner"]), shared))[0])
            except Exception as exc:  # noqa: BLE001
                out.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        [t.start() for t in threads]
        [t.join(timeout=90) for t in threads]
        statuses = sorted(r["status"] for r in out if isinstance(r, dict))
        assert statuses == ["applied", "applied"], out
        assert ChecklistResponse.objects.count() == 1 and SyncReceipt.objects.count() == 1
        assert ChecklistResponse.objects.get().revision == 1


@pytest.mark.django_db
class TestResolveConflict:
    def conflict(self, w):
        a = oissued(w)
        c = api(w["runner"])
        sync(c, op(w, a, "FRIDGE", "4"))
        sync(api(w["runner2"]), op(w, a, "FRIDGE", "5", base=1))
        lost = op(w, a, "FRIDGE", "9", base=1)
        assert results(sync(c, lost))[0]["status"] == "conflict"
        return a, c, lost

    def resolve(self, c, w, a, lost, resolution, **extra):
        return c.post(BASE + "resolve-conflict/", {"operation_id": str(uuid.uuid4()), "conflict_operation_id": lost["operation_id"], "resolution": resolution,
                                                    "version_id": str(w["oversion"].pk), "item_id": lost["item_id"], **extra}, format="json")

    def test_use_local_after_review_is_revalidated_and_audited(self, w):
        a, c, lost = self.conflict(w)
        r = self.resolve(c, w, a, lost, "use_local", value="9", base_revision=2)
        assert r.status_code == 200 and r.json()["status"] == "applied" and r.json()["resolution"] == "use_local"
        assert str(ChecklistResponse.objects.get().value).startswith("9") and ChecklistResponse.objects.get().revision == 3
        assert ActivityLog.objects.filter(verb="execution.conflict_resolved", metadata__resolution="use_local").count() == 1

    def test_use_local_with_an_invalid_value_is_refused(self, w):
        a, c, lost = self.conflict(w)
        r = self.resolve(c, w, a, lost, "use_local", value="99", base_revision=2)
        assert r.json()["status"] == "rejected" and r.json()["code"] == "validation_failed"
        assert str(ChecklistResponse.objects.get().value).startswith("5")

    def test_use_local_with_a_stale_base_conflicts_again(self, w):
        a, c, lost = self.conflict(w)
        assert self.resolve(c, w, a, lost, "use_local", value="9", base_revision=1).json()["status"] == "conflict"

    def test_keep_server_discards_nothing_on_the_server_and_is_audited(self, w):
        a, c, lost = self.conflict(w)
        r = self.resolve(c, w, a, lost, "keep_server")
        assert r.json()["status"] == "applied" and str(r.json()["server"]["value"]).startswith("5")
        assert str(ChecklistResponse.objects.get().value).startswith("5") and ChecklistResponse.objects.get().revision == 2
        assert ActivityLog.objects.filter(verb="execution.conflict_resolved", metadata__resolution="keep_server").count() == 1

    def test_only_a_conflict_the_user_received_can_be_resolved(self, w):
        a, c, lost = self.conflict(w)
        assert self.resolve(api(w["runner2"]), w, a, lost, "keep_server").json()["code"] == "unknown_conflict"
        assert self.resolve(c, w, a, {**lost, "operation_id": str(uuid.uuid4())}, "keep_server").json()["code"] == "unknown_conflict"
        assert self.resolve(c, w, a, lost, "merge").json()["code"] == "invalid_operation"

    def test_a_submitted_execution_conflict_is_not_resolvable(self, w):
        a = oissued(w)
        ex, _ = services.start_or_resume(a, actor=w["runner"])
        services.save_answers(ex, actor=w["runner"], inputs=[services.AnswerInput(oid(w, c), v) for c, v in (("FIRE-EXIT", "yes"), ("HANDWASH", "yes"), ("FRIDGE", "4"))])
        services.submit(ex, actor=w["runner"])
        lost = op(w, a, "FIRE-EXIT", "no", base=1)
        assert results(sync(api(w["runner"]), lost))[0]["code"] == "execution_submitted"
        r = self.resolve(api(w["runner"]), w, a, lost, "use_local", value="no", base_revision=1)
        assert r.json()["code"] == "not_resolvable"
        assert ChecklistResponse.objects.get(item__code="FIRE-EXIT").value == "yes"

    def test_resolution_is_idempotent(self, w):
        a, c, lost = self.conflict(w)
        resolution_id = str(uuid.uuid4())
        body = {"operation_id": resolution_id, "conflict_operation_id": lost["operation_id"], "resolution": "use_local", "version_id": str(w["oversion"].pk),
                "item_id": lost["item_id"], "value": "9", "base_revision": 2}
        first = c.post(BASE + "resolve-conflict/", body, format="json").json()
        second = c.post(BASE + "resolve-conflict/", body, format="json").json()
        assert first["status"] == "applied" and second["duplicate"] is True
        assert ChecklistResponse.objects.get().revision == 3 and ActivityLog.objects.filter(verb="execution.conflict_resolved").count() == 1


@pytest.mark.django_db
class TestOnlineRegression:
    def test_phase_9_saving_and_submission_still_work_beside_offline(self, w):
        a = oissued(w)
        ex, _ = services.start_or_resume(a, actor=w["runner"])
        r = services.save_answers(ex, actor=w["runner"], inputs=[services.AnswerInput(oid(w, "FRIDGE"), "4")])
        assert r.ok
        out = results(sync(api(w["runner"]), op(w, a, "FRIDGE", "6", base=1)))[0]  # an offline write continues from the online revision
        assert out["status"] == "applied" and out["response_revision"] == 2

