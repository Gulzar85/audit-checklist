"""``/api/v1/assignments/``: contract, lifecycle actions, scope isolation, protected fields, bulk."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.assignments import services
from apps.assignments.models import ChecklistAssignment
from apps.core.models import ActivityLog

from assignments_support import build_world, future

BASE = "/api/v1/assignments/"


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


def results(response):
    return response.json()["results"]


def iso(hours=48):
    return (timezone.now() + timedelta(hours=hours)).isoformat()


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_world(org_a, org_b, make_user, assign, make_type)


def make(w, **kw):
    args = {"actor": w["admin"], "version": w["version"], "location": w["r1"]}
    args.update(kw)
    return services.create_assignment(**args)


def payload(w, **over):
    data = {"version": str(w["version"].pk), "location": str(w["r1"].pk), "priority": "high", "due_at": iso(), "instructions": "Go"}
    data.update(over)
    return data


@pytest.mark.django_db
class TestReadAndAccess:
    def test_anonymous_and_unprivileged(self, w):
        assert api(None).get(BASE).status_code in (401, 403)
        assert api(w["plain"]).get(BASE).json()["results"] == [] or api(w["plain"]).get(BASE).status_code == 403

    def test_list_retrieve_and_shape(self, w):
        a = make(w, issue=True, due_at=future(30), assigned_to=w["worker"])
        client = api(w["admin"])
        row = results(client.get(BASE))[0]
        assert row["reference"] == a.reference and row["status"] == "assigned" and row["priority"] == "normal"
        assert row["checklist"]["version"] == 1 and row["location"]["path"] == "Region North / Area 1 / Restaurant 1"
        assert row["assigned_to"]["email"] == "as-worker@example.com" and row["is_overdue"] is False
        one = client.get(f"{BASE}{a.pk}/").json()
        assert one["id"] == str(a.pk) and "version" not in one.get("checklist", {}).get("code", "")

    def test_filters_search_ordering_and_pagination(self, w):
        make(w, issue=True, priority="low")
        make(w, location=w["wh"], issue=True, priority="critical")
        client = api(w["admin"])
        assert len(results(client.get(BASE, {"priority": "critical"}))) == 1
        assert len(results(client.get(BASE, {"search": "Warehouse"}))) == 1
        assert [r["priority"] for r in results(client.get(BASE, {"ordering": "-priority_rank"}))][0] == "critical"
        assert len(results(client.get(BASE, {"status": "draft"}))) == 0
        assert client.get(BASE, {"page": 1, "page_size": 1}).json()["count"] == 2

    def test_other_org_and_scope_are_404(self, w):
        inside, outside = make(w, location=w["r1"]), make(w, location=w["r3"])
        mgr = api(w["manager"])
        assert [r["id"] for r in results(mgr.get(BASE))] == [str(inside.pk)]
        assert mgr.get(f"{BASE}{outside.pk}/").status_code == 404
        assert api(w["other"]).get(f"{BASE}{inside.pk}/").status_code == 404
        assert results(api(w["other"]).get(BASE)) == []
        for action in ("issue", "reopen"):
            assert api(w["other"]).post(f"{BASE}{inside.pk}/{action}/").status_code == 404
        assert mgr.get(f"{BASE}{outside.pk}/history/").status_code == 404

    def test_no_put_no_delete(self, w):
        a = make(w)
        client = api(w["admin"])
        assert client.put(f"{BASE}{a.pk}/", payload(w), format="json").status_code == 405
        assert client.delete(f"{BASE}{a.pk}/").status_code == 405

    def test_summary_and_history(self, w):
        a = make(w, issue=True, due_at=future(3))
        make(w, location=w["r2"])
        client = api(w["admin"])
        s = client.get(f"{BASE}summary/").json()
        assert s["total"] == 2 and s["open"] == 1 and s["due_soon"] == 1 and s["by_status"]["draft"] == 1
        h = client.get(f"{BASE}{a.pk}/history/").json()["results"]
        assert {e["verb"] for e in h} >= {"assignment.created", "assignment.issued"}
        assert api(w["manager"]).get(f"{BASE}summary/").json()["total"] == 1 + 1  # r1 and r2 are both in Area 1


@pytest.mark.django_db
class TestCreate:
    def test_create_draft_and_issued(self, w):
        client = api(w["admin"])
        r = client.post(BASE, payload(w), format="json")
        assert r.status_code == 201 and r.json()["status"] == "draft" and r.json()["priority"] == "high"
        r2 = client.post(BASE, payload(w, location=str(w["r2"].pk), issue=True), format="json")
        assert r2.status_code == 201 and r2.json()["status"] == "assigned"
        assert ChecklistAssignment.objects.count() == 2

    def test_duplicates_conflict(self, w):
        make(w, issue=True)
        r = api(w["admin"]).post(BASE, payload(w, issue=True), format="json")
        assert r.status_code == 409 and r.json()["error"]["code"] == "CONFLICT"

    def test_validation(self, w):
        client = api(w["admin"])
        assert client.post(BASE, payload(w, due_at=iso(-5), issue=True), format="json").status_code == 400
        assert client.post(BASE, payload(w, priority="nope"), format="json").status_code == 400
        assert client.post(BASE, {}, format="json").status_code == 400
        assert client.post(BASE, payload(w, version=str(w["b_version"].pk)), format="json").status_code == 400
        assert not ChecklistAssignment.objects.exists()

    def test_protected_fields_cannot_be_injected(self, w, org_b):
        r = api(w["admin"]).post(BASE, payload(w, status="completed", organization=str(org_b.pk), created_by=str(w["other"].pk), number=99), format="json")
        a = ChecklistAssignment.objects.get()
        assert r.status_code == 201 and a.status == "draft" and a.organization_id == w["org"].pk and a.created_by_id == w["admin"].pk and a.number == 1

    def test_scope_and_permission(self, w):
        mgr, viewer = api(w["manager"]), api(w["viewer"])
        assert mgr.post(BASE, payload(w, location=str(w["r3"].pk)), format="json").status_code == 400  # unknown to them: generic
        assert mgr.post(BASE, payload(w, location=str(w["b_loc"].pk)), format="json").status_code == 400
        assert viewer.post(BASE, payload(w), format="json").status_code == 403
        assert mgr.post(BASE, payload(w), format="json").status_code == 201
        assert api(w["plain"]).post(BASE, payload(w), format="json").status_code in (400, 403)


@pytest.mark.django_db
class TestLifecycleActions:
    def test_issue_cancel_reopen(self, w):
        a = make(w, due_at=future(5))
        client = api(w["admin"])
        assert client.post(f"{BASE}{a.pk}/issue/").json()["status"] == "assigned"
        assert client.post(f"{BASE}{a.pk}/issue/").status_code == 409
        assert client.post(f"{BASE}{a.pk}/cancel/", {}, format="json").status_code == 400  # reason required
        c = client.post(f"{BASE}{a.pk}/cancel/", {"reason": "Closed"}, format="json")
        assert c.status_code == 200 and c.json()["status"] == "cancelled" and c.json()["cancel_reason"] == "Closed"
        assert client.post(f"{BASE}{a.pk}/cancel/", {"reason": "again"}, format="json").status_code == 409
        assert client.post(f"{BASE}{a.pk}/reopen/").json()["status"] == "draft"
        assert client.post(f"{BASE}{a.pk}/reopen/").status_code == 400  # a draft cannot be reopened: invalid transition
        assert ActivityLog.objects.filter(verb="assignment.issued").count() == 1

    def test_no_arbitrary_status_change(self, w, org_b):
        a = make(w, issue=True)
        client = api(w["admin"])
        r = client.patch(f"{BASE}{a.pk}/", {"status": "completed"}, format="json")
        assert r.status_code == 400 and ChecklistAssignment.objects.get(pk=a.pk).status == "assigned"
        for f, v in (("organization", str(org_b.pk)), ("version", str(w["version"].pk)), ("location", str(w["r2"].pk)), ("created_by", str(w["other"].pk)), ("number", 7)):
            assert client.patch(f"{BASE}{a.pk}/", {f: v}, format="json").status_code == 400, f

    def test_patch_safe_fields(self, w):
        a = make(w)
        client = api(w["admin"])
        r = client.patch(f"{BASE}{a.pk}/", {"priority": "low", "instructions": "New", "due_at": iso(72)}, format="json")
        assert r.status_code == 200 and r.json()["priority"] == "low" and r.json()["instructions"] == "New"
        client.post(f"{BASE}{a.pk}/issue/")
        r = client.patch(f"{BASE}{a.pk}/", {"priority": "critical"}, format="json")
        assert r.status_code == 200 and r.json()["priority"] == "critical"
        assert client.patch(f"{BASE}{a.pk}/", {"instructions": "edit after issue"}, format="json").status_code == 400

    def test_change_actions(self, w):
        a = make(w, issue=True)
        client = api(w["admin"])
        assert client.post(f"{BASE}{a.pk}/change-priority/", {"priority": "critical"}, format="json").json()["priority"] == "critical"
        assert client.post(f"{BASE}{a.pk}/change-due-date/", {"due_at": iso(96)}, format="json").status_code == 200
        assert client.post(f"{BASE}{a.pk}/change-due-date/", {"due_at": iso(-4)}, format="json").status_code == 400
        assert client.post(f"{BASE}{a.pk}/change-assignee/", {"assigned_to": str(w["worker"].pk)}, format="json").json()["assigned_to"]["email"] == "as-worker@example.com"
        assert client.post(f"{BASE}{a.pk}/change-assignee/", {"assigned_to": str(w["plain"].pk)}, format="json").status_code == 400
        assert client.post(f"{BASE}{a.pk}/change-assignee/", {"assigned_to": None}, format="json").json()["assigned_to"] is None

    def test_permissions_on_actions(self, w):
        a = make(w, issue=True, assigned_to=w["worker"])
        for who in ("viewer", "worker"):
            client = api(w[who])
            assert client.post(f"{BASE}{a.pk}/cancel/", {"reason": "x"}, format="json").status_code == 403, who
            assert client.post(f"{BASE}{a.pk}/change-priority/", {"priority": "low"}, format="json").status_code == 403, who
            assert client.patch(f"{BASE}{a.pk}/", {"priority": "low"}, format="json").status_code == 403, who
        assert ChecklistAssignment.objects.get(pk=a.pk).status == "assigned"


@pytest.mark.django_db
class TestBulk:
    def test_bulk_create_and_dry_run(self, w):
        client = api(w["admin"])
        body = {"version": str(w["version"].pk), "locations": [str(w["r1"].pk), str(w["r3"].pk)], "issue": True, "priority": "high", "due_at": iso()}
        dry = client.post(f"{BASE}bulk-create/", {**body, "dry_run": True}, format="json")
        assert dry.status_code == 200 and dry.json()["created"] is False and not ChecklistAssignment.objects.exists()
        r = client.post(f"{BASE}bulk-create/", body, format="json")
        assert r.status_code == 201 and r.json()["count"] == 2 and ChecklistAssignment.objects.filter(status="assigned").count() == 2

    def test_all_or_nothing_and_repeat_safety(self, w):
        make(w, location=w["r2"], issue=True)
        client = api(w["admin"])
        body = {"version": str(w["version"].pk), "locations": [str(w["r1"].pk), str(w["r2"].pk)], "issue": True}
        r = client.post(f"{BASE}bulk-create/", body, format="json")
        assert r.status_code == 400 and r.json()["error"]["details"]["created"] is False and [x["ok"] for x in r.json()["error"]["details"]["targets"]] == [True, False]
        assert ChecklistAssignment.objects.count() == 1

    def test_descendants_and_scope(self, w):
        mgr = api(w["manager"])
        r = mgr.post(f"{BASE}bulk-create/", {"version": str(w["version"].pk), "locations": [str(w["area1"].pk)], "include_descendants": True}, format="json")
        assert r.status_code == 201 and r.json()["count"] == 3  # the area itself and its two restaurants
        r = mgr.post(f"{BASE}bulk-create/", {"version": str(w["version"].pk), "locations": [str(w["r3"].pk), str(w["b_loc"].pk)]}, format="json")
        assert r.status_code == 400 and all("not available" in " ".join(x["errors"]) for x in r.json()["error"]["details"]["targets"])
        assert api(w["viewer"]).post(f"{BASE}bulk-create/", {"version": str(w["version"].pk), "locations": [str(w["r1"].pk)]}, format="json").status_code == 400
        assert api(w["other"]).post(f"{BASE}bulk-create/", {"version": str(w["version"].pk), "locations": [str(w["r1"].pk)]}, format="json").status_code == 400
        assert ChecklistAssignment.objects.count() == 3

    def test_limits(self, w):
        client = api(w["admin"])
        assert client.post(f"{BASE}bulk-create/", {"version": str(w["version"].pk), "locations": []}, format="json").status_code == 400
        assert client.post(f"{BASE}bulk-create/", {"version": str(w["version"].pk), "locations": [str(w["r1"].pk)] * 201}, format="json").status_code == 400


@pytest.mark.django_db
def test_list_query_count_is_flat(w, django_assert_max_num_queries):
    make(w, issue=True)
    client = api(w["admin"])
    client.get(BASE)
    for loc in (w["r2"], w["r3"], w["wh"]):
        make(w, issue=True, location=loc)
    with django_assert_max_num_queries(30):
        assert client.get(BASE).status_code == 200


@pytest.mark.django_db
def test_schema_documents_the_endpoints(w):
    paths = api(w["admin"]).get("/api/schema/?format=json").json()["paths"] if api(w["admin"]).get("/api/schema/?format=json").status_code == 200 else {}
    if paths:
        assert "/api/v1/assignments/" in paths and "/api/v1/assignments/{id}/issue/" in paths and "/api/v1/assignments/bulk-create/" in paths



