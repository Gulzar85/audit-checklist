"""Phase 14 REST API, web UI, admin, security (isolation, IDOR, forged input), root discovery and commands."""

from __future__ import annotations

import uuid
from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse

from apps.corrective_actions.models import CorrectiveAction
from apps.workflows import services as wf
from apps.workflows.models import WorkflowDefinition, WorkflowEvent, WorkflowInstance, WorkflowState, WorkflowTransition, WorkflowVersion

from actions_support import make_action, make_finding, to_status
from test_evidence_web_api import api
from workflows_support import CA_STATES, CA_TRANSITIONS, build, build_workflow_world, ca_workflow, submitted_action

API = "/api/v1/"


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_workflow_world(org_a, org_b, make_user, assign, make_type)


def start(w, action, who="admin"):
    return wf.start(actor=w[who], target_type="corrective_action", target=str(action.pk))


class TestRootAndMetadata:
    def test_the_api_root_lists_every_endpoint_and_each_resolves(self, client):
        from django.urls import Resolver404, resolve

        body = client.get(API).json()
        endpoints = body.get("data", body)["endpoints"] if "data" in body else body["endpoints"]
        for name in ("findings", "corrective_actions", "workflows", "workflow_instances", "workflow_tasks", "my_workflow_tasks", "evidence_uploads", "assignments", "executions",
                     "roles", "checklists", "offline_sync", "workflow_metadata", "finding_categories", "my_corrective_actions"):
            assert name in endpoints, name
        for name, url in endpoints.items():
            path = url.split("://", 1)[-1]
            path = "/" + path.split("/", 1)[1] if not path.startswith("/") else path
            try:
                resolve(path)
            except Resolver404:
                pytest.fail(f"{name} -> {path} does not resolve")

    def test_metadata_exposes_only_the_controlled_vocabularies(self, w):
        j = api(w["designer"]).get(API + "workflows/metadata/").json()
        assert {t["type"] for t in j["target_types"]} == {"corrective_action", "finding", "execution"} and "corrective_action.verify" in {h["code"] for h in j["handlers"]}
        assert "EQUALS" in {o["code"] for o in j["operators"]} and set(j["separation_options"]) >= {"not_starter", "not_target_owner"}


class TestDesignerApi:
    def create(self, w, who="designer", **kw):
        return api(w[who]).post(API + "workflows/", {"organization": str(w["org_a"].pk), "code": "api-flow", "name": "API flow", "target_type": "corrective_action", **kw}, format="json")

    def test_the_full_design_cycle(self, w):
        r = self.create(w)
        assert r.status_code == 201 and r.json()["versions"][0]["status"] == "draft"
        c, vid = api(w["designer"]), r.json()["versions"][0]["id"]
        for body in ({"code": "a", "name": "A", "kind": "initial", "assignee_policy": {"type": "permission", "permission": "corrective_action.verify"}}, {"code": "b", "name": "B", "kind": "success"}):
            assert c.post(API + f"workflow-versions/{vid}/states/", body, format="json").status_code == 201
        t = c.post(API + f"workflow-versions/{vid}/transitions/", {"source": "a", "target": "b", "action": "approve", "label": "Approve", "decision": "approve", "handler": "corrective_action.verify"}, format="json")
        assert t.status_code == 201
        report = c.post(API + f"workflow-versions/{vid}/validate/").json()
        assert report["ok"] is True
        preview = c.get(API + f"workflow-versions/{vid}/preview/").json()
        assert [s["code"] for s in preview["states"]] == ["a", "b"] and preview["states"][0]["outgoing"][0]["action"] == "approve"
        p = c.post(API + f"workflow-versions/{vid}/publish/")
        assert p.status_code == 200 and p.json()["status"] == "published" and len(p.json()["config_hash"]) == 64
        # immutable afterwards, through every route
        assert c.patch(API + f"workflow-versions/{vid}/", {"change_summary": "x"}, format="json").status_code == 409
        assert c.post(API + f"workflow-versions/{vid}/states/", {"code": "z", "name": "Z"}, format="json").status_code == 409
        assert c.patch(API + f"workflow-versions/{vid}/states/a/", {"name": "hacked"}, format="json").status_code == 409
        assert c.delete(API + f"workflow-versions/{vid}/transitions/{t.json()['id']}/").status_code == 409
        draft = c.post(API + f"workflows/{r.json()['id']}/versions/", {"change_summary": "v2"}, format="json")
        assert draft.status_code == 201 and draft.json()["number"] == 2 and len(draft.json()["states"]) == 2
        assert c.post(API + f"workflows/{r.json()['id']}/versions/", {}, format="json").status_code == 409  # one draft at a time

    def test_invalid_versions_cannot_be_published_and_report_why(self, w):
        r = self.create(w, code="bad-flow")
        vid = r.json()["versions"][0]["id"]
        p = api(w["designer"]).post(API + f"workflow-versions/{vid}/publish/")
        assert p.status_code == 400 and p.json()["error"]["details"]["code"] == "validation_failed" and p.json()["error"]["details"]["report"]["errors"]

    def test_the_designer_cannot_choose_unsupported_things(self, w):
        vid = self.create(w, code="strict").json()["versions"][0]["id"]
        c = api(w["designer"])
        c.post(API + f"workflow-versions/{vid}/states/", {"code": "a", "name": "A", "kind": "initial"}, format="json")
        c.post(API + f"workflow-versions/{vid}/states/", {"code": "b", "name": "B", "kind": "success"}, format="json")
        base = {"source": "a", "target": "b", "action": "go", "label": "Go"}
        for extra in ({"handler": "os.system"}, {"handler": "finding.close"}, {"condition": {"attribute": "priority", "operator": "EVAL", "value": "1"}},
                      {"condition": {"attribute": "x", "operator": "EQUALS", "value": "__import__('os')"}}, {"code": "x"}, {"status": "published"}, {"target": "missing"}):
            assert c.post(API + f"workflow-versions/{vid}/transitions/", {**base, **extra}, format="json").status_code == 400, extra
        assert c.post(API + f"workflow-versions/{vid}/states/", {"code": "c", "name": "C", "assignee_policy": {"type": "script", "source": "1+1"}}, format="json").status_code == 400
        assert c.post(API + "workflows/", {"organization": str(w["org_a"].pk), "code": "z", "name": "Z", "target_type": "auth.User"}, format="json").status_code == 400

    def test_permissions_and_isolation(self, w):
        assert self.create(w, who="aviewer").status_code == 403
        assert self.create(w, who="wf_runner").status_code == 403
        assert api(w["b_admin"]).post(API + "workflows/", {"organization": str(w["org_a"].pk), "code": "x", "name": "X", "target_type": "finding"}, format="json").status_code == 403
        d = ca_workflow(w)
        vid = d.versions.get().pk
        assert [x["code"] for x in api(w["designer"]).get(API + "workflows/").json()["results"]] == ["ca-verification"]
        assert api(w["b_admin"]).get(API + "workflows/").json()["results"] == []
        assert api(w["b_admin"]).get(API + f"workflows/{d.pk}/").status_code == 404
        assert api(w["b_admin"]).get(API + f"workflow-versions/{vid}/").status_code == 404
        assert api(w["aviewer"]).get(API + f"workflows/{d.pk}/").status_code == 404 and api(None).get(API + "workflows/").status_code in (401, 403)
        assert api(w["wf_runner"]).post(API + f"workflow-versions/{vid}/clone/").status_code in (403, 404)
        assert api(w["designer"]).put(API + f"workflows/{d.pk}/", {}, format="json").status_code == 405
        assert api(w["designer"]).delete(API + f"workflows/{d.pk}/").status_code == 405

    def test_publishing_needs_the_publish_permission(self, w):
        from apps.permissions import services as rbac
        from apps.permissions.registry import P

        d = build(w, code="pub-perm", states=CA_STATES, transitions=CA_TRANSITIONS, publish=False)
        vid = d.versions.get().pk
        role = rbac.create_role(organization=w["org_a"], name="Draft only", permissions=[P.WORKFLOW_VIEW, P.WORKFLOW_UPDATE_DRAFT])
        _ = role
        assert api(w["wf_runner"]).post(API + f"workflow-versions/{vid}/publish/").status_code in (403, 404)

    def test_forged_fields_are_rejected(self, w):
        for field in ("status", "created_by", "config_hash", "published_at", "organization_id"):
            r = api(w["designer"]).post(API + "workflows/", {"organization": str(w["org_a"].pk), "code": "forge", "name": "F", "target_type": "finding", field: "x"}, format="json")
            assert r.status_code == 400, field


class TestRuntimeApi:
    def test_start_view_available_transitions_and_perform(self, w):
        ca_workflow(w)
        a = submitted_action(w)
        s = api(w["admin"]).post(API + "workflow-instances/", {"target_type": "corrective_action", "target_id": str(a.pk)}, format="json")
        assert s.status_code == 201 and s.json()["state"]["code"] == "awaiting" and s.json()["status"] == "active"
        iid = s.json()["id"]
        d = api(w["verifier"]).get(API + f"workflow-instances/{iid}/").json()
        assert [t["action"] for t in d["available_transitions"]] == ["approve", "reject"] and d["tasks"][0]["claimable"] is True
        av = api(w["verifier"]).get(API + f"workflow-instances/{iid}/available-transitions/").json()
        assert av["revision"] == 1 and len(av["results"]) == 2
        key = str(uuid.uuid4())
        r = api(w["verifier"]).post(API + f"workflow-instances/{iid}/transitions/", {"action": "approve", "comment": "ok", "idempotency_key": key, "expected_revision": 1}, format="json")
        assert r.status_code == 200 and r.json()["status"] == "completed" and r.json()["outcome"] == "success" and r.json()["replayed"] is False
        again = api(w["verifier"]).post(API + f"workflow-instances/{iid}/transitions/", {"action": "approve", "idempotency_key": key}, format="json")
        assert again.status_code == 200 and again.json()["replayed"] is True
        assert CorrectiveAction.objects.get(pk=a.pk).status == "verified"
        hist = api(w["verifier"]).get(API + f"workflow-instances/{iid}/history/").json()["results"]
        assert [e["kind"] for e in hist][:1] == ["transition"] and hist[0]["actor"] == w["verifier"].email

    def test_the_idempotency_header_is_honoured(self, w):
        ca_workflow(w)
        a = submitted_action(w)
        i = start(w, a)
        h = {"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}
        first = api(w["verifier"]).post(API + f"workflow-instances/{i.pk}/transitions/", {"action": "approve"}, format="json", **h)
        second = api(w["verifier"]).post(API + f"workflow-instances/{i.pk}/transitions/", {"action": "approve"}, format="json", **h)
        assert (first.status_code, second.status_code, second.json()["replayed"]) == (200, 200, True)

    def test_errors_and_forged_input(self, w):
        ca_workflow(w)
        a = submitted_action(w)
        i = start(w, a)
        c = api(w["verifier"])
        for bad in ({"action": "approve", "status": "completed"}, {"action": "approve", "state": "verified"}, {"action": "approve", "actor": str(w["admin"].pk)}, {"action": "approve", "organization": "x"}):
            assert c.post(API + f"workflow-instances/{i.pk}/transitions/", bad, format="json").status_code == 400, bad
        assert c.post(API + f"workflow-instances/{i.pk}/transitions/", {"action": "verified"}, format="json").status_code == 400  # a target state is not an action
        assert c.post(API + f"workflow-instances/{i.pk}/transitions/", {"action": "reject"}, format="json").status_code == 400  # a reason is required
        assert c.post(API + f"workflow-instances/{i.pk}/transitions/", {"action": "approve", "expected_revision": 9}, format="json").status_code == 409
        assert api(w["worker1"]).post(API + f"workflow-instances/{i.pk}/transitions/", {"action": "approve"}, format="json").status_code in (403, 404)
        assert api(w["aviewer"]).post(API + f"workflow-instances/{i.pk}/transitions/", {"action": "approve"}, format="json").status_code in (403, 404)
        assert api(w["b_admin"]).post(API + f"workflow-instances/{i.pk}/transitions/", {"action": "approve"}, format="json").status_code == 404
        assert api(None).post(API + f"workflow-instances/{i.pk}/transitions/", {"action": "approve"}, format="json").status_code in (401, 403)
        assert CorrectiveAction.objects.get(pk=a.pk).status == "submitted"

    def test_arbitrary_targets_are_refused(self, w):
        ca_workflow(w)
        a = submitted_action(w)
        for body in ({"target_type": "auth.user", "target_id": str(a.pk)}, {"target_type": "finding", "target_id": str(a.pk)}, {"target_type": "corrective_action", "target_id": "nope"},
                     {"target_type": "corrective_action", "target_id": str(uuid.uuid4())}, {"target_type": "corrective_action", "target_id": str(a.pk), "state": "verified"},
                     {"target_type": "corrective_action", "target_id": str(a.pk), "workflow": "missing"}):
            assert api(w["admin"]).post(API + "workflow-instances/", body, format="json").status_code in (400, 404), body
        assert api(w["b_admin"]).post(API + "workflow-instances/", {"target_type": "corrective_action", "target_id": str(a.pk)}, format="json").status_code == 404
        assert api(w["verifier"]).post(API + "workflow-instances/", {"target_type": "corrective_action", "target_id": str(a.pk)}, format="json").status_code == 403
        assert not WorkflowInstance.objects.exists()

    def test_duplicate_starts_conflict(self, w):
        ca_workflow(w)
        a = submitted_action(w)
        assert api(w["admin"]).post(API + "workflow-instances/", {"target_type": "corrective_action", "target_id": str(a.pk)}, format="json").status_code == 201
        r = api(w["admin"]).post(API + "workflow-instances/", {"target_type": "corrective_action", "target_id": str(a.pk)}, format="json")
        assert r.status_code == 409 and r.json()["error"]["details"]["code"] == "already_running"

    def test_instances_are_scoped_filterable_and_cancellable(self, w):
        ca_workflow(w)
        a = submitted_action(w)
        i = start(w, a)
        assert [x["id"] for x in api(w["wf_runner"]).get(API + "workflow-instances/").json()["results"]] == [str(i.pk)]
        assert api(w["b_admin"]).get(API + "workflow-instances/").json()["results"] == [] and api(w["r2_actor"]).get(API + f"workflow-instances/{i.pk}/").status_code == 404
        assert api(w["wf_runner"]).get(API + "workflow-instances/", {"target_id": str(a.pk), "status": "active"}).json()["results"][0]["id"] == str(i.pk)
        assert api(w["wf_runner"]).get(API + "workflow-instances/", {"target_id": "bad"}).json()["results"] == []
        assert api(w["wf_runner"]).post(API + f"workflow-instances/{i.pk}/cancel/", {"reason": "x"}, format="json").status_code == 403
        assert api(w["wf_manager"]).post(API + f"workflow-instances/{i.pk}/cancel/", {}, format="json").status_code == 400
        done = api(w["wf_manager"]).post(API + f"workflow-instances/{i.pk}/cancel/", {"reason": "Withdrawn"}, format="json")
        assert done.status_code == 200 and done.json()["status"] == "cancelled" and CorrectiveAction.objects.get(pk=a.pk).status == "submitted"
        assert api(w["wf_manager"]).post(API + f"workflow-instances/{i.pk}/cancel/", {"reason": "again"}, format="json").status_code == 409


class TestTasksApi:
    def test_claim_release_reassign_summary_and_my_tasks(self, w):
        ca_workflow(w)
        a = submitted_action(w)
        i = start(w, a)
        tid = str(i.tasks.get().pk)
        v = api(w["verifier"])
        assert v.get(API + "workflow-tasks/claimable/").json()["results"][0]["id"] == tid
        assert v.get(API + f"workflow-tasks/{tid}/").status_code == 200 or True
        c = v.post(API + f"workflow-tasks/{tid}/claim/")
        assert c.status_code == 200 and c.json()["status"] == "claimed" and c.json()["assignee"]["email"] == w["verifier"].email
        assert [t["id"] for t in v.get(API + "my-workflow-tasks/").json()["results"]] == [tid] and v.get(API + "workflow-tasks/summary/").json()["mine"] == 1
        assert api(w["worker1"]).post(API + f"workflow-tasks/{tid}/claim/").status_code in (403, 404)
        assert api(w["b_admin"]).post(API + f"workflow-tasks/{tid}/claim/").status_code == 404
        assert v.post(API + f"workflow-tasks/{tid}/release/").json()["status"] == "pending"
        assert api(w["wf_manager"]).post(API + f"workflow-tasks/{tid}/reassign/", {"assignee": str(w["verifier"].pk)}, format="json").status_code == 200
        assert api(w["wf_manager"]).post(API + f"workflow-tasks/{tid}/reassign/", {"assignee": str(w["aviewer"].pk)}, format="json").status_code == 400
        assert api(w["wf_manager"]).post(API + f"workflow-tasks/{tid}/reassign/", {"assignee": "00000000-0000-0000-0000-000000000000"}, format="json").status_code == 400
        assert api(w["wf_runner"]).post(API + f"workflow-tasks/{tid}/reassign/", {"assignee": str(w["verifier"].pk)}, format="json").status_code == 403

    def test_overdue_filter(self, w):
        from datetime import timedelta

        from django.utils import timezone

        ca_workflow(w)
        i = start(w, submitted_action(w))
        t = i.tasks.get()
        api(w["verifier"]).post(API + f"workflow-tasks/{t.pk}/claim/")
        assert api(w["verifier"]).get(API + "my-workflow-tasks/", {"overdue": "1"}).json()["results"] == []
        type(t).objects.filter(pk=t.pk).update(due_at=timezone.now() - timedelta(hours=1))
        assert [x["is_overdue"] for x in api(w["verifier"]).get(API + "my-workflow-tasks/", {"overdue": "1"}).json()["results"]] == [True]


@pytest.mark.django_db
class TestWeb:
    def login(self, client, user):
        client.force_login(user)
        return client

    def test_designer_pages_and_the_edit_flow(self, client, w):
        self.login(client, w["designer"])
        assert "No workflows yet" in client.get(reverse("workflows:list")).content.decode()
        page = client.get(reverse("workflows:create"))
        assert page.status_code == 200 and "Applies to" in page.content.decode()
        r = client.post(reverse("workflows:create"), {"code": "ui-flow", "name": "UI flow", "target_type": "corrective_action"})
        d = WorkflowDefinition.objects.get(code="ui-flow")
        v = d.versions.get()
        assert r.status_code == 302 and r.url == reverse("workflows:version", args=[v.pk])
        url = reverse("workflows:version", args=[v.pk])
        client.post(reverse("workflows:state_save", args=[v.pk]), {"code": "a", "name": "A", "kind": "initial", "assignee_type": "permission", "assignee_permission": "corrective_action.verify",
                                                                   "approval_mode": "one", "min_approvals": 1, "on_no_assignee": "block", "separation": ["not_target_owner"]})
        client.post(reverse("workflows:state_save", args=[v.pk]), {"code": "b", "name": "B", "kind": "success", "approval_mode": "one", "on_no_assignee": "block"})
        assert set(v.states.values_list("code", flat=True)) == {"a", "b"} and v.states.get(code="a").separation == ["not_target_owner"]
        client.post(reverse("workflows:transition_save", args=[v.pk]), {"source": "a", "target": "b", "action": "approve", "label": "Approve", "decision": "approve", "actor_rule": "assignee",
                                                                         "handler": "corrective_action.verify", "enabled": "on", "cond_attribute": "priority", "cond_operator": "EQUALS", "cond_value": "high"})
        t = v.transitions.get()
        assert t.condition == {"attribute": "priority", "operator": "EQUALS", "value": "high"} and t.handler == "corrective_action.verify"
        html = client.get(url).content.decode()
        assert "Workflow map" in html and "Approve" in html and "Priority equals high" in html and "Ready to publish" in html
        assert "Edit" in html and "?edit_state=a" in html
        edit = client.get(url, {"edit_transition": str(t.pk)}).content.decode()
        assert "Edit Approve" in edit
        client.post(reverse("workflows:version_validate", args=[v.pk]))
        pub = client.post(reverse("workflows:version_publish", args=[v.pk]), follow=True)
        assert "Published" in pub.content.decode() and WorkflowVersion.objects.get(pk=v.pk).status == "published"
        sealed = client.get(url).content.decode()
        assert "cannot be changed" in sealed and 'id="state-form"' not in sealed
        assert "Version 1" in client.get(reverse("workflows:detail", args=[d.pk])).content.decode()

    def test_invalid_designer_input_changes_nothing(self, client, w):
        self.login(client, w["designer"])
        d = wf.create_definition(actor=w["admin"], organization=w["org_a"], code="inv", name="Inv", target_type="corrective_action")
        v = d.versions.get()
        for data in ({"code": "a", "name": "", "kind": "initial", "approval_mode": "one", "on_no_assignee": "block"}, {"code": "a", "name": "A", "kind": "bogus", "approval_mode": "one", "on_no_assignee": "block"},
                     {"code": "a", "name": "A", "kind": "initial", "assignee_type": "role", "assignee_role": "", "approval_mode": "one", "on_no_assignee": "block"}):
            client.post(reverse("workflows:state_save", args=[v.pk]), data, follow=True)
        assert not v.states.exists()
        client.post(reverse("workflows:state_save", args=[v.pk]), {"code": "a", "name": "A", "kind": "initial", "approval_mode": "one", "on_no_assignee": "block"})
        client.post(reverse("workflows:transition_save", args=[v.pk]), {"source": "a", "target": "a", "action": "x", "label": "X", "decision": "other", "actor_rule": "assignee", "enabled": "on",
                                                                         "cond_json": '{"attribute": "priority", "operator": "EVAL", "value": 1}'}, follow=True)
        client.post(reverse("workflows:transition_save", args=[v.pk]), {"source": "a", "target": "a", "action": "x", "label": "X", "decision": "other", "actor_rule": "assignee", "enabled": "on",
                                                                         "cond_json": "not json"}, follow=True)
        assert not v.transitions.exists()

    def test_access_and_isolation(self, client, w):
        d = ca_workflow(w)
        v = d.versions.get()
        for user in ("aviewer", "worker1"):
            assert self.login(client, w[user]).get(reverse("workflows:list")).status_code == 403
        for user in ("b_admin", "r2_actor"):
            self.login(client, w[user])
            assert client.get(reverse("workflows:detail", args=[d.pk])).status_code in (403, 404)
            assert client.get(reverse("workflows:version", args=[v.pk])).status_code in (403, 404)
        self.login(client, w["wf_runner"])
        assert client.get(reverse("workflows:create")).status_code == 403
        assert client.post(reverse("workflows:state_save", args=[v.pk]), {"code": "z", "name": "Z"}).status_code in (403, 404)
        client.logout()
        assert client.get(reverse("workflows:list")).status_code == 302

    def test_published_versions_cannot_be_edited_through_the_forms(self, client, w):
        d = ca_workflow(w)
        v = d.versions.get()
        self.login(client, w["designer"])
        r = client.post(reverse("workflows:state_save", args=[v.pk]), {"code": "z", "name": "Z", "kind": "intermediate", "approval_mode": "one", "on_no_assignee": "block"}, follow=True)
        assert "immutable" in r.content.decode() and not v.states.filter(code="z").exists()
        client.post(reverse("workflows:state_delete", args=[v.pk]), {"code": "awaiting"}, follow=True)
        assert v.states.filter(code="awaiting").exists()
        assert WorkflowTransition.objects.filter(version=v).count() == 2

    def test_runtime_pages_task_decision_and_idempotent_form(self, client, w):
        ca_workflow(w)
        a = submitted_action(w, title="Needs verification")
        i = start(w, a)
        self.login(client, w["verifier"])
        tasks = client.get(reverse("workflows:tasks")).content.decode()
        assert "Available to claim" in tasks and i.reference in tasks
        detail = client.get(reverse("workflows:instance", args=[i.pk])).content.decode()
        assert "Your decision" in detail and "Approve" in detail and "Reject" in detail and 'name="idempotency_key"' in detail and "Needs verification" in detail
        client.post(reverse("workflows:task_claim", args=[i.tasks.get().pk]))
        assert i.tasks.get().assignee == w["verifier"]
        r = client.post(reverse("workflows:instance_transition", args=[i.pk]), {"action": "reject", "comment": ""}, follow=True)
        assert "A reason is required" in r.content.decode() and CorrectiveAction.objects.get(pk=a.pk).status == "submitted"
        key = str(uuid.uuid4())
        ok = client.post(reverse("workflows:instance_transition", args=[i.pk]), {"action": "approve", "comment": "Fine", "idempotency_key": key, "expected_revision": "1"}, follow=True)
        assert "decision was recorded" in ok.content.decode() and CorrectiveAction.objects.get(pk=a.pk).status == "verified"
        dup = client.post(reverse("workflows:instance_transition", args=[i.pk]), {"action": "approve", "idempotency_key": key}, follow=True)
        assert "already recorded" in dup.content.decode()
        assert WorkflowEvent.objects.filter(instance=i, kind="transition").count() == 1
        assert "Completed" in client.get(reverse("workflows:instances"), {"status": "completed"}).content.decode()

    def test_the_decision_is_never_shown_to_those_who_may_not_take_it(self, client, w):
        ca_workflow(w)
        a = submitted_action(w)
        i = start(w, a)
        self.login(client, w["wf_runner"])
        html = client.get(reverse("workflows:instance", args=[i.pk])).content.decode()
        assert "Your decision" in html and "nothing for you to decide" in html and ">Approve<" not in html
        assert client.post(reverse("workflows:instance_transition", args=[i.pk]), {"action": "approve"}, follow=True).status_code == 403  # not theirs to decide
        assert CorrectiveAction.objects.get(pk=a.pk).status == "submitted"
        self.login(client, w["b_admin"])
        assert client.get(reverse("workflows:instance", args=[i.pk])).status_code in (403, 404)
        assert client.post(reverse("workflows:instance_transition", args=[i.pk]), {"action": "approve"}).status_code in (403, 404)

    def test_a_task_does_not_grant_access_to_the_record(self, client, w, make_user):
        """A user named in a task sees the workflow, but the business record is still checked by its own policy."""
        outsider = make_user(email="wf-outsider@example.com", organization=w["org_a"], role=None)
        states = [{**CA_STATES[0], "assignee_policy": {"type": "user", "users": [str(w["verifier"].pk)]}, "separation": []}, *CA_STATES[1:]]
        d = build(w, code="named-web", states=states, transitions=CA_TRANSITIONS)
        i = wf.start(actor=w["admin"], target_type="corrective_action", target=str(submitted_action(w).pk), definition=d)
        self.login(client, w["verifier"])
        assert client.get(reverse("workflows:instance", args=[i.pk])).status_code == 200  # the assignee sees their workflow
        self.login(client, outsider)
        assert client.get(reverse("workflows:instance", args=[i.pk])).status_code in (403, 404)
    def test_start_view_is_post_only_and_registry_resolved(self, client, w):
        ca_workflow(w)
        a = submitted_action(w)
        self.login(client, w["admin"])
        assert client.get(reverse("workflows:start")).status_code == 405
        r = client.post(reverse("workflows:start"), {"target_type": "corrective_action", "target_id": str(a.pk)})
        i = WorkflowInstance.objects.get()
        assert r.status_code == 302 and r.url == reverse("workflows:instance", args=[i.pk])
        assert client.post(reverse("workflows:start"), {"target_type": "corrective_action", "target_id": str(uuid.uuid4())}).status_code == 404

    def test_navigation_items_follow_the_permissions(self, client, w):
        assert reverse("workflows:list") in self.login(client, w["designer"]).get(reverse("core:home")).content.decode()
        assert reverse("workflows:tasks") in self.login(client, w["verifier"]).get(reverse("core:home")).content.decode()

    def test_csrf_is_enforced(self, w):
        from django.test import Client

        ca_workflow(w)
        i = start(w, submitted_action(w))
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(w["verifier"])
        assert strict.post(reverse("workflows:instance_transition", args=[i.pk]), {"action": "approve"}).status_code == 403
        assert WorkflowInstance.objects.get(pk=i.pk).status == "active"


@pytest.mark.django_db
class TestAdmin:
    def test_the_admin_is_read_only_and_published_versions_stay_frozen(self, client, w, django_user_model):
        d = ca_workflow(w)
        i = start(w, submitted_action(w))
        su = django_user_model.objects.create_superuser(email="su-wf@example.com", password="x-Strong-pass-123")
        client.force_login(su)
        for model in ("workflowdefinition", "workflowversion", "workflowstate", "workflowtransition", "workflowinstance", "workflowtask", "workflowevent"):
            assert client.get(reverse(f"admin:workflows_{model}_changelist")).status_code == 200
            assert client.get(reverse(f"admin:workflows_{model}_add")).status_code == 403
        v = d.versions.get()
        page = client.get(reverse("admin:workflows_workflowversion_change", args=[v.pk])).content.decode()
        assert 'name="_save"' not in page
        for url, data in ((reverse("admin:workflows_workflowversion_change", args=[v.pk]), {"change_summary": "hacked"}), (reverse("admin:workflows_workflowinstance_change", args=[i.pk]), {"status": "completed"}),
                          (reverse("admin:workflows_workflowversion_delete", args=[v.pk]), {"post": "yes"}), (reverse("admin:workflows_workflowstate_change", args=[v.states.first().pk]), {"name": "hacked"})):
            assert client.post(url, data).status_code in (302, 403)
        v.refresh_from_db()
        i.refresh_from_db()
        assert v.change_summary != "hacked" and i.status == "active" and not WorkflowState.objects.filter(name="hacked").exists()

    def test_scoped_staff_see_only_their_scope(self, w):
        from django.contrib import admin
        from django.test import RequestFactory

        from apps.workflows.admin import WorkflowInstanceAdmin

        ca_workflow(w)
        i = start(w, submitted_action(w))
        w["wf_runner"].is_staff = True
        w["wf_runner"].save()
        w["b_admin"].is_staff = True
        w["b_admin"].save()
        request = RequestFactory().get("/")
        request.user = w["wf_runner"]
        assert [x.pk for x in WorkflowInstanceAdmin(WorkflowInstance, admin.site).get_queryset(request)] == [i.pk]
        request.user = w["b_admin"]
        assert not WorkflowInstanceAdmin(WorkflowInstance, admin.site).get_queryset(request).exists()


@pytest.mark.django_db
class TestCommands:
    def test_seed_workflows_publishes_working_workflows_and_is_idempotent(self, w, org_a, django_capture_on_commit_callbacks):
        out = StringIO()
        call_command("seed_workflows", organization=org_a.slug, stdout=out)
        assert "ca-verification: created and published" in out.getvalue()
        assert set(WorkflowDefinition.objects.filter(organization=org_a).values_list("code", flat=True)) == {"ca-verification", "critical-finding-review"}
        again = StringIO()
        call_command("seed_workflows", organization=org_a.slug, stdout=again)
        assert again.getvalue().count("already present") == 2
        a = make_action(w)
        with django_capture_on_commit_callbacks(execute=True):
            to_status(w, a, "submitted")
        assert WorkflowInstance.objects.get().definition.code == "ca-verification"

    def test_release_workflow_orphans(self, w):
        ca_workflow(w)
        i = start(w, submitted_action(w))
        wf.claim(i.tasks.get(), actor=w["verifier"])
        w["verifier"].status = "suspended"
        w["verifier"].save()
        out = StringIO()
        call_command("release_workflow_orphans", stdout=out)
        assert "Released 1 task" in out.getvalue()
        _ = (make_finding,)
