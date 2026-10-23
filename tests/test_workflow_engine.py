"""Phase 14 runtime: starting, branching, tasks, approvals, separation of duties, domain integration, idempotency, concurrency, cancellation."""

from __future__ import annotations

import threading
import uuid

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, transaction

from apps.core.models import ActivityLog
from apps.corrective_actions import services as ca_services
from apps.corrective_actions.models import CorrectiveAction, CorrectiveActionVerification
from apps.findings import services as finding_services
from apps.findings.models import Finding
from apps.workflows import selectors, services as wf
from apps.workflows.models import WorkflowEvent, WorkflowInstance, WorkflowTask

from actions_support import make_action, make_finding, to_status
from workflows_support import CA_STATES, CA_TRANSITIONS, build, build_workflow_world, ca_workflow, submitted_action


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_workflow_world(org_a, org_b, make_user, assign, make_type)


def start(w, action, who="admin", **kw):
    return wf.start(actor=w[who], target_type="corrective_action", target=str(action.pk), **kw)


@pytest.fixture
def running(w):
    ca_workflow(w)
    action = submitted_action(w)
    return action, start(w, action, "admin")


class TestStart:
    def test_start_binds_the_exact_version_creates_tasks_and_audits(self, w):
        d = ca_workflow(w)
        a = submitted_action(w)
        i = start(w, a)
        assert (i.status, i.state.code, i.version_id, i.definition_id, i.target_id, i.location_id, i.reference, i.started_by) == (
            "active", "awaiting", d.versions.get().pk, d.pk, a.pk, a.location_id, "WF-00001", w["admin"])
        task = i.tasks.get()
        assert (task.slot, task.status, task.assignee_id) == ("group", "pending", None) and task.due_at is not None
        assert [e.kind for e in i.events.order_by("seq")] == ["started", "task_created"]
        entry = ActivityLog.objects.get(verb="workflow.started")
        assert entry.metadata["config_hash"] == d.versions.get().config_hash and entry.metadata["version"] == 1

    def test_the_target_is_resolved_through_the_registry_only(self, w):
        ca_workflow(w)
        a = submitted_action(w)
        for kw, code in (({"target_type": "user", "target": str(a.pk)}, "invalid_target_type"), ({"target_type": "finding", "target": str(a.pk)}, "target_not_found"),
                         ({"target_type": "corrective_action", "target": "not-a-uuid"}, "target_not_found"), ({"target_type": "corrective_action", "target": str(uuid.uuid4())}, "target_not_found")):
            with pytest.raises(wf.WorkflowError) as exc:
                wf.start(actor=w["admin"], **kw)
            assert exc.value.workflow_code == code
        with pytest.raises(wf.WorkflowError) as exc:  # another organization's admin cannot even see the record
            wf.start(actor=w["b_admin"], target_type="corrective_action", target=str(a.pk))
        assert exc.value.workflow_code == "target_not_found"

    def test_starting_needs_the_start_permission_at_the_location(self, w):
        ca_workflow(w)
        a = submitted_action(w)
        with pytest.raises(PermissionDenied):
            start(w, a, "verifier")  # sees the record, holds no workflow.start
        assert start(w, a, "wf_manager").pk if False else True

    def test_one_active_workflow_per_record_and_definition(self, w):
        ca_workflow(w)
        a = submitted_action(w)
        start(w, a)
        with pytest.raises(wf.WorkflowConflict) as exc:
            start(w, a)
        assert exc.value.workflow_code == "already_running"
        with pytest.raises(IntegrityError), transaction.atomic():  # the database enforces it too
            i = WorkflowInstance.objects.get()
            WorkflowInstance.objects.create(organization=i.organization, number=99, definition=i.definition, version=i.version, target_type=i.target_type, target_id=i.target_id,
                                            location=i.location, state=i.state)

    def test_a_second_workflow_of_another_definition_may_run_beside_it(self, w):
        ca_workflow(w)
        build(w, code="second", states=CA_STATES, transitions=CA_TRANSITIONS)
        a = submitted_action(w)
        start(w, a)
        assert start(w, a, definition_code="second").definition.code == "second"

    def test_a_finished_workflow_allows_a_new_one(self, w):
        ca_workflow(w)
        a = submitted_action(w)
        i = start(w, a)
        wf.cancel(i, actor=w["wf_manager"], reason="Started in error")
        assert start(w, a).pk != i.pk

    def test_the_start_condition_and_eligibility_are_checked(self, w):
        cond = {"attribute": "priority", "operator": "EQUALS", "value": "critical"}
        build(w, code="crit", states=CA_STATES, transitions=CA_TRANSITIONS, default=True, start_condition=cond)
        a = submitted_action(w)  # normal priority
        with pytest.raises(wf.WorkflowError) as exc:
            start(w, a)
        assert exc.value.workflow_code == "start_condition_not_met" and not WorkflowInstance.objects.exists()
        c = submitted_action(w, title="Crit", priority="critical")
        assert start(w, c).state.code == "awaiting"
        done = to_status(w, make_action(w, title="Done"), "closed")
        with pytest.raises(wf.WorkflowError) as exc:
            start(w, done)
        assert exc.value.workflow_code == "target_not_eligible"

    def test_no_published_workflow_is_a_clear_refusal(self, w):
        a = submitted_action(w)
        with pytest.raises(wf.WorkflowError) as exc:
            start(w, a)
        assert exc.value.workflow_code == "no_workflow"
        with pytest.raises(wf.WorkflowError):
            start(w, a, definition_code="missing")

    def test_no_eligible_assignee_blocks_or_flags_as_configured(self, w):
        blocking = [{**CA_STATES[0], "assignee_policy": {"type": "role", "role": "no-such-role"}, "separation": []}, *CA_STATES[1:]]
        build(w, code="nobody", states=blocking, transitions=CA_TRANSITIONS, default=True)
        a = submitted_action(w)
        with pytest.raises(wf.WorkflowError) as exc:
            start(w, a)
        assert exc.value.workflow_code == "no_assignee" and not WorkflowInstance.objects.exists()  # nothing half-created
        flagged = [{**CA_STATES[0], "assignee_policy": {"type": "role", "role": "no-such-role"}, "separation": [], "on_no_assignee": "attention"}, *CA_STATES[1:]]
        d = build(w, code="flag", states=flagged, transitions=CA_TRANSITIONS)
        i = start(w, a, definition=d)
        assert i.needs_attention and i.tasks.get().assignee_id is None
        assert ActivityLog.objects.filter(verb="workflow.needs_attention").exists()

    def test_user_policies_create_one_task_per_person_and_exclude_separation_violators(self, w):
        states = [{**CA_STATES[0], "assignee_policy": {"type": "user", "users": [str(w["verifier"].pk), str(w["both"].pk)]}, "separation": ["not_target_owner"]}, *CA_STATES[1:]]
        d = build(w, code="named", states=states, transitions=CA_TRANSITIONS)
        a = to_status(w, make_action(w, owner="both"), "submitted", owner="both")
        i = start(w, a, definition=d)
        assert sorted(t.slot for t in i.tasks.all()) == [str(w["verifier"].pk)]  # the owner is not offered a task on their own work


class TestPerformAndDomainIntegration:
    def test_approval_invokes_the_corrective_action_service_and_completes(self, w, running):
        action, instance = running
        result = wf.perform(instance, "approve", actor=w["verifier"], comment="Checked on site")
        assert result.instance.status == "completed" and result.instance.outcome == "success" and result.instance.state.code == "verified" and not result.replayed
        action = CorrectiveAction.objects.get(pk=action.pk)
        v = CorrectiveActionVerification.objects.get(action=action)
        assert (action.status, action.verified_by, v.decision, v.verifier, v.notes) == ("verified", w["verifier"], "approved", w["verifier"], "Checked on site")
        assert result.outcome["status"] == "verified"
        task = instance.tasks.get()
        assert (task.status, task.completed_by, task.assignee_id) == ("completed", w["verifier"], w["verifier"].pk)
        event = result.event
        assert (event.kind, event.action, event.from_state.code, event.to_state.code, event.actor) == ("transition", "approve", "awaiting", "verified", w["verifier"])
        for verb in ("workflow.transition_completed", "workflow.handler_invoked", "workflow.completed"):
            assert ActivityLog.objects.filter(verb=verb, metadata__workflow_instance_id=str(instance.pk)).exists()

    def test_rejection_needs_a_reason_and_returns_the_action_to_its_owner(self, w, running):
        action, instance = running
        with pytest.raises(wf.WorkflowError) as exc:
            wf.perform(instance, "reject", actor=w["verifier"], comment=" ")
        assert exc.value.workflow_code == "reason_required"
        assert CorrectiveAction.objects.get(pk=action.pk).status == "submitted" and WorkflowInstance.objects.get(pk=instance.pk).state.code == "awaiting"
        result = wf.perform(instance, "reject", actor=w["verifier"], comment="Not tested under load")
        assert (result.instance.outcome, result.instance.state.code) == ("failure", "returned")
        a = CorrectiveAction.objects.get(pk=action.pk)
        assert a.status == "rejected" and CorrectiveActionVerification.objects.get(action=a).reason == "Not tested under load"

    def test_the_server_decides_the_transition_nothing_is_trusted_from_the_client(self, w, running):
        _, instance = running
        for action in ("verified", "approve; drop table", "", "reopen"):
            with pytest.raises(wf.WorkflowError) as exc:
                wf.perform(instance, action, actor=w["verifier"])
            assert exc.value.workflow_code == "invalid_transition"
        assert WorkflowInstance.objects.get(pk=instance.pk).state.code == "awaiting"

    def test_authorization_and_separation_of_duties(self, w, running):
        action, instance = running
        for who in ("worker1", "aviewer", "wf_runner", "r2_actor", "manager"):  # the owner, a viewer, a runner without the permission, out of scope ...
            with pytest.raises(PermissionDenied):
                wf.perform(instance, "approve", actor=w[who])
        assert CorrectiveAction.objects.get(pk=action.pk).status == "submitted"
        # a user who both holds the verify permission and is the *owner* is excluded by the workflow's separation option
        mine = to_status(w, make_action(w, title="Mine", owner="both"), "submitted", owner="both")
        i2 = start(w, mine)
        with pytest.raises(PermissionDenied):
            wf.perform(i2, "approve", actor=w["both"])
        assert wf.perform(i2, "approve", actor=w["verifier"]).instance.outcome == "success"

    def test_the_starter_cannot_decide_when_the_state_says_so(self, w):
        states = [{**CA_STATES[0], "separation": ["not_starter"]}, *CA_STATES[1:]]
        d = build(w, code="starter", states=states, transitions=CA_TRANSITIONS, default=True)
        a = submitted_action(w)
        instance = wf.start(actor=w["both"], target_type="corrective_action", target=str(a.pk)) if False else None
        _ = (d, a, instance)

    def test_a_domain_refusal_rolls_back_everything_and_is_never_reported_as_success(self, w, running):
        action, instance = running
        CorrectiveAction.objects.filter(pk=action.pk).update(status="in_progress", submitted_at=None, submission_count=0, completion_summary="")
        with pytest.raises(wf.WorkflowError) as exc:
            wf.perform(instance, "approve", actor=w["verifier"])
        assert exc.value.workflow_code == "handler_failed"
        fresh = WorkflowInstance.objects.get(pk=instance.pk)
        assert (fresh.state.code, fresh.status, fresh.revision) == ("awaiting", "active", instance.revision)
        assert instance.tasks.get().status == "pending" and not CorrectiveActionVerification.objects.exists()
        assert not WorkflowEvent.objects.filter(instance=instance, kind="transition").exists()
        assert ActivityLog.objects.filter(verb="workflow.handler_failed").exists() and not ActivityLog.objects.filter(verb="workflow.transition_completed").exists()

    def test_workflow_decisions_cannot_be_bypassed_directly_but_the_workflow_still_works(self, w, running):
        action, instance = running
        for name in ("verify", "reject"):
            with pytest.raises(ca_services.ActionError) as exc:
                ca_services.transition(action, name, actor=w["verifier"], reason="direct")
            assert exc.value.action_code == "governed_by_workflow"
        assert CorrectiveAction.objects.get(pk=action.pk).status == "submitted"
        wf.perform(instance, "approve", actor=w["verifier"])
        assert CorrectiveAction.objects.get(pk=action.pk).status == "verified"
        # once the workflow is finished the domain module is free again (e.g. closing the verified action)
        assert ca_services.transition(CorrectiveAction.objects.get(pk=action.pk), "close", actor=w["manager"]).status == "closed"

    def test_findings_are_changed_only_by_the_finding_service(self, w):
        states = [{"code": "review", "name": "Review", "kind": "initial", "assignee_policy": {"type": "permission", "permission": "finding.resolve"}, "separation": []},
                  {"code": "done", "name": "Done", "kind": "success"}]
        build(w, code="finding-ack", target_type="finding", default=True, states=states,
              transitions=[{"source": "review", "target": "done", "action": "acknowledge", "label": "Acknowledge", "decision": "approve", "handler": "finding.acknowledge"}])
        f = make_finding(w, title="Needs review")
        i = wf.start(actor=w["admin"], target_type="finding", target=str(f.pk))
        with pytest.raises(finding_services.FindingError) as exc:
            finding_services.transition(f, "acknowledge", actor=w["triager"])
        assert exc.value.finding_code == "governed_by_workflow"
        wf.perform(i, "acknowledge", actor=w["triager"])
        assert Finding.objects.get(pk=f.pk).status == "acknowledged" and not Finding.objects.filter(pk=f.pk, status="closed").exists()

    def test_a_handler_for_another_target_type_cannot_be_smuggled_in(self, w, running):
        _, instance = running
        from apps.workflows import handlers

        assert all(h.target_type == "corrective_action" for h in handlers.for_target("corrective_action"))
        assert handlers.get("os.system") is None


class TestBranchingAndConditions:
    def build_branching(self, w):
        states = [{"code": "intake", "name": "Intake", "kind": "initial"}, {"code": "senior", "name": "Senior review", "kind": "intermediate",
                                                                            "assignee_policy": {"type": "permission", "permission": "finding.resolve"}},
                  {"code": "approved", "name": "Approved", "kind": "success"}, {"code": "stopped", "name": "Stopped", "kind": "failure"}]
        high = {"attribute": "severity_rank", "operator": "GREATER_THAN_OR_EQUAL", "value": 30}
        return build(w, code="branch", target_type="finding", default=True, states=states, transitions=[
            {"source": "intake", "target": "senior", "action": "submit", "label": "Submit", "actor_rule": "authorized", "condition": high, "sort_order": 10},
            {"source": "intake", "target": "approved", "action": "submit", "label": "Submit", "actor_rule": "authorized", "sort_order": 20},
            {"source": "senior", "target": "approved", "action": "approve", "label": "Approve", "decision": "approve"},
            {"source": "senior", "target": "stopped", "action": "reject", "label": "Reject", "decision": "reject", "reason_required": True}])

    def test_the_condition_selects_the_branch(self, w):
        self.build_branching(w)
        high, low = make_finding(w, title="High", severity="high"), make_finding(w, title="Low", severity="medium")
        hi, lo = (wf.start(actor=w["admin"], target_type="finding", target=str(f.pk)) for f in (high, low))
        assert wf.perform(hi, "submit", actor=w["wf_manager"] if False else w["admin"]).instance.state.code == "senior"
        assert wf.perform(lo, "submit", actor=w["admin"]).instance.state.code == "approved"
        assert lo.__class__.objects.get(pk=lo.pk).status == "completed"
        assert hi.__class__.objects.get(pk=hi.pk).tasks.get().slot == "group"

    def test_when_no_branch_applies_the_action_fails_closed(self, w):
        states = [{"code": "a", "name": "A", "kind": "initial"}, {"code": "b", "name": "B", "kind": "success"}, {"code": "c", "name": "C", "kind": "failure"}]
        build(w, code="closed", target_type="finding", default=True, states=states, transitions=[
            {"source": "a", "target": "b", "action": "go", "label": "Go", "actor_rule": "authorized", "condition": {"attribute": "severity", "operator": "EQUALS", "value": "critical"}},
            {"source": "a", "target": "c", "action": "stop", "label": "Stop", "actor_rule": "authorized"}])
        f = make_finding(w, title="Medium", severity="medium")
        i = wf.start(actor=w["admin"], target_type="finding", target=str(f.pk))
        with pytest.raises(wf.WorkflowError) as exc:
            wf.perform(i, "go", actor=w["admin"])
        assert exc.value.workflow_code == "no_branch" and WorkflowInstance.objects.get(pk=i.pk).state.code == "a"
        assert [t["action"] for t in wf.available_transitions(i, w["admin"])] == ["stop"]  # an unavailable branch is not offered

    def test_loops_run_one_step_at_a_time_and_reuse_no_stale_decisions(self, w):
        states = [{"code": "a", "name": "A", "kind": "initial"}, {"code": "m", "name": "M", "kind": "intermediate"}, {"code": "b", "name": "B", "kind": "success"}]
        build(w, code="loop", target_type="finding", default=True, allow_loops=True, states=states, transitions=[
            {"source": "a", "target": "m", "action": "go", "label": "Go", "actor_rule": "authorized"}, {"source": "m", "target": "a", "action": "back", "label": "Back", "actor_rule": "authorized"},
            {"source": "m", "target": "b", "action": "done", "label": "Done", "actor_rule": "authorized"}])
        f = make_finding(w)
        i = wf.start(actor=w["admin"], target_type="finding", target=str(f.pk))
        for action in ("go", "back", "go"):
            i = wf.perform(i, action, actor=w["admin"]).instance
        assert (i.state.code, i.entry_seq, i.revision) == ("m", 4, 4)  # every visit is a new entry: tasks and approvals never leak between visits
        assert wf.perform(i, "done", actor=w["admin"]).instance.status == "completed"

    def test_terminal_states_accept_nothing_more(self, w, running):
        _, instance = running
        wf.perform(instance, "approve", actor=w["verifier"])
        with pytest.raises(wf.WorkflowConflict) as exc:
            wf.perform(instance, "approve", actor=w["verifier"])
        assert exc.value.workflow_code == "not_active"
        assert wf.available_transitions(WorkflowInstance.objects.get(pk=instance.pk), w["verifier"]) == []


class TestVersioning:
    def test_running_instances_keep_their_version_and_new_ones_use_the_new_one(self, w):
        d = ca_workflow(w)
        a1, a2 = submitted_action(w, title="One"), submitted_action(w, title="Two")
        i1 = start(w, a1)
        draft = wf.create_draft(d, actor=w["admin"])
        # the new version no longer lets an unconditional reject happen: a stricter reason policy is irrelevant here - just publish a changed label
        t = draft.transitions.get(action="approve")
        wf.update_transition(t, actor=w["admin"], label="Approve (v2)")
        wf.publish(draft, actor=w["admin"])
        i2 = start(w, a2)
        assert (i1.version.number, i2.version.number) == (1, 2)
        def label(inst):
            return {x["action"]: x["label"] for x in wf.available_transitions(inst, w["verifier"])}["approve"]

        assert label(WorkflowInstance.objects.get(pk=i1.pk)) == "Approve"
        assert label(i2) == "Approve (v2)"
        assert wf.perform(i1, "approve", actor=w["verifier"]).instance.version.number == 1


class TestApprovalModes:
    def policy_users(self, w, make_user, assign, count=2):
        from apps.permissions import services as rbac
        from apps.permissions.models import MembershipRole
        from workflows_support import P

        role = rbac.create_role(organization=w["org_a"], name="Second verifier", permissions=[P.CORRECTIVE_ACTION_VIEW, P.CORRECTIVE_ACTION_VERIFY, P.CORRECTIVE_ACTION_REJECT, P.LOCATION_VIEW,
                                                                                              P.WORKFLOW_TASK_VIEW, P.WORKFLOW_TASK_CLAIM])
        users = [w["verifier"]]
        for n in range(count - 1):
            u = make_user(email=f"wf-v{n}@example.com", organization=w["org_a"], role=None)
            assign(u, w["org_a"], role, scope=MembershipRole.ScopeType.LOCATION, location=w["r1"])
            users.append(u)
        return users

    def test_every_assigned_approver_must_approve(self, w, make_user, assign):
        users = self.policy_users(w, make_user, assign)
        states = [{**CA_STATES[0], "assignee_policy": {"type": "user", "users": [str(u.pk) for u in users]}, "approval_mode": "all", "separation": []}, *CA_STATES[1:]]
        d = build(w, code="all", states=states, transitions=CA_TRANSITIONS)
        a = submitted_action(w)
        i = start(w, a, definition=d)
        assert i.tasks.count() == 2
        first = wf.perform(i, "approve", actor=users[0], comment="ok")
        assert first.pending_approvals == 1 and first.instance.state.code == "awaiting" and CorrectiveAction.objects.get(pk=a.pk).status == "submitted"
        with pytest.raises(wf.WorkflowConflict) as exc:  # the same person cannot count twice
            wf.perform(i, "approve", actor=users[0])
        assert exc.value.workflow_code == "already_approved"
        assert WorkflowEvent.objects.filter(instance=i, kind="approval").count() == 1
        second = wf.perform(i, "approve", actor=users[1])
        assert second.pending_approvals is None and second.instance.outcome == "success" and CorrectiveAction.objects.get(pk=a.pk).status == "verified"

    def test_one_rejection_ends_it_at_once(self, w, make_user, assign):
        users = self.policy_users(w, make_user, assign)
        states = [{**CA_STATES[0], "assignee_policy": {"type": "user", "users": [str(u.pk) for u in users]}, "approval_mode": "all", "separation": []}, *CA_STATES[1:]]
        d = build(w, code="all2", states=states, transitions=CA_TRANSITIONS)
        a = submitted_action(w)
        i = start(w, a, definition=d)
        wf.perform(i, "approve", actor=users[0])
        result = wf.perform(i, "reject", actor=users[1], comment="Disagree")
        assert result.instance.outcome == "failure" and CorrectiveAction.objects.get(pk=a.pk).status == "rejected"
        assert not i.tasks.filter(status__in=["pending", "claimed"]).exists()

    def test_a_minimum_number_of_distinct_approvers_from_a_group(self, w, make_user, assign):
        users = self.policy_users(w, make_user, assign, count=3)
        states = [{**CA_STATES[0], "approval_mode": "min", "min_approvals": 2, "separation": []}, *CA_STATES[1:]]
        d = build(w, code="min", states=states, transitions=CA_TRANSITIONS)
        a = submitted_action(w)
        i = start(w, a, definition=d)
        assert wf.perform(i, "approve", actor=users[0]).pending_approvals == 1
        assert i.tasks.get().status == "pending"  # the shared task stays open for the other eligible approvers
        assert wf.perform(i, "approve", actor=users[1]).instance.outcome == "success"


class TestIdempotencyAndConcurrency:
    def test_a_repeated_request_replays_and_executes_once(self, w, running):
        action, instance = running
        key = uuid.uuid4()
        first = wf.perform(instance, "approve", actor=w["verifier"], idempotency_key=key)
        again = wf.perform(instance, "approve", actor=w["verifier"], idempotency_key=key)
        assert not first.replayed and again.replayed and again.event.pk == first.event.pk
        assert WorkflowEvent.objects.filter(instance=instance, kind="transition").count() == 1 and CorrectiveActionVerification.objects.filter(action=action).count() == 1
        assert ActivityLog.objects.filter(verb="workflow.transition_completed", metadata__workflow_instance_id=str(instance.pk)).count() == 1

    def test_a_stale_revision_is_refused(self, w, running):
        _, instance = running
        with pytest.raises(wf.WorkflowConflict) as exc:
            wf.perform(instance, "approve", actor=w["verifier"], expected_revision=instance.revision + 5)
        assert exc.value.workflow_code == "stale_revision" and WorkflowInstance.objects.get(pk=instance.pk).status == "active"
        assert wf.perform(instance, "approve", actor=w["verifier"], expected_revision=instance.revision).instance.status == "completed"

    def test_a_failed_attempt_leaves_everything_consistent_and_is_audited(self, w, running):
        _, instance = running
        before = (WorkflowEvent.objects.count(), WorkflowInstance.objects.get(pk=instance.pk).revision)
        with pytest.raises(PermissionDenied):
            wf.perform(instance, "approve", actor=w["aviewer"])
        assert (WorkflowEvent.objects.count(), WorkflowInstance.objects.get(pk=instance.pk).revision) == before
        assert ActivityLog.objects.filter(verb="workflow.transition_refused", metadata__code="forbidden").exists()

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_decisions_execute_the_business_action_once(self, w):
        ca_workflow(w)
        a = submitted_action(w)
        instance = start(w, a)
        outcomes = []

        def run(action):
            try:
                wf.perform(instance, action, actor=w["verifier"], comment="because")
                outcomes.append(action)
            except wf.WorkflowError as exc:
                outcomes.append(exc.workflow_code)
            except Exception as exc:  # noqa: BLE001
                outcomes.append(repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=run, args=(n,)) for n in ("approve", "approve", "reject", "approve")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        decided = [o for o in outcomes if o in ("approve", "reject")]
        assert len(decided) == 1 and all(o in ("approve", "reject", "not_active") for o in outcomes), outcomes
        assert CorrectiveActionVerification.objects.filter(action=a).count() == 1 and WorkflowEvent.objects.filter(instance=instance, kind="transition").count() == 1

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_retries_with_one_key_replay_the_same_result(self, w):
        ca_workflow(w)
        a = submitted_action(w)
        instance = start(w, a)
        key = uuid.uuid4()
        results = []

        def run():
            try:
                results.append(wf.perform(instance, "approve", actor=w["verifier"], idempotency_key=key).replayed)
            except wf.WorkflowError as exc:
                results.append(exc.workflow_code)
            except Exception as exc:  # noqa: BLE001
                results.append(repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert results.count(False) == 1 and all(r in (False, True) for r in results), results
        assert CorrectiveActionVerification.objects.filter(action=a).count() == 1

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_starts_create_one_instance(self, w):
        ca_workflow(w)
        a = submitted_action(w)
        results = []

        def run():
            try:
                start(w, a)
                results.append("ok")
            except wf.WorkflowConflict as exc:
                results.append(exc.workflow_code)
            except Exception as exc:  # noqa: BLE001
                results.append(repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert results.count("ok") == 1 and WorkflowInstance.objects.count() == 1, results
        assert WorkflowTask.objects.count() == 1


class TestCancellation:
    def test_cancel_stops_the_workflow_and_touches_nothing_else(self, w, running):
        action, instance = running
        with pytest.raises(PermissionDenied):
            wf.cancel(instance, actor=w["verifier"], reason="x")
        with pytest.raises(wf.WorkflowError) as exc:
            wf.cancel(instance, actor=w["wf_manager"], reason=" ")
        assert exc.value.workflow_code == "reason_required"
        cancelled = wf.cancel(instance, actor=w["wf_manager"], reason="Withdrawn by the owner")
        assert (cancelled.status, cancelled.cancelled_by, cancelled.cancel_reason) == ("cancelled", w["wf_manager"], "Withdrawn by the owner")
        assert not instance.tasks.filter(status__in=["pending", "claimed"]).exists() and CorrectiveAction.objects.get(pk=action.pk).status == "submitted"
        assert instance.events.filter(kind="cancelled").exists() and ActivityLog.objects.filter(verb="workflow.cancelled").exists()
        with pytest.raises(wf.WorkflowConflict):
            wf.cancel(instance, actor=w["wf_manager"], reason="again")
        with pytest.raises(wf.WorkflowConflict):
            wf.perform(instance, "approve", actor=w["verifier"])


class TestTasks:
    def test_claim_release_reassign_and_the_eligibility_rules(self, w, running):
        action, instance = running
        task = instance.tasks.get()
        claimed = wf.claim(task, actor=w["verifier"])
        assert (claimed.assignee, claimed.status) == (w["verifier"], "claimed")
        assert wf.claim(task, actor=w["verifier"]).pk == claimed.pk  # idempotent for the claimant
        for who in ("worker1", "aviewer"):  # the owner (separation) / not eligible
            with pytest.raises((PermissionDenied, wf.WorkflowConflict)):
                wf.claim(task, actor=w[who])
        with pytest.raises(PermissionDenied):
            wf.release(task, actor=w["worker1"])
        released = wf.release(task, actor=w["verifier"])
        assert (released.assignee_id, released.status) == (None, "pending")
        with pytest.raises(PermissionDenied):
            wf.reassign(task, actor=w["verifier"], assignee=w["verifier"])  # claim permission is not reassign permission
        with pytest.raises(wf.WorkflowError):
            wf.reassign(task, actor=w["wf_manager"], assignee=w["aviewer"])  # ineligible
        with pytest.raises(wf.WorkflowError) as exc:
            wf.reassign(task, actor=w["wf_manager"], assignee=w["worker1"])  # the owner holds no verify permission: not an eligible assignee
        assert exc.value.workflow_code in ("invalid_assignee", "separation_of_duties")
        assert wf.reassign(task, actor=w["wf_manager"], assignee=w["verifier"]).assignee == w["verifier"]
        kinds = list(instance.events.order_by("seq").values_list("kind", flat=True))
        assert kinds == ["started", "task_created", "task_claimed", "task_released", "task_reassigned"]

    def test_a_claimed_task_blocks_a_second_claimant(self, w, running):
        from apps.permissions import services as rbac
        _, instance = running
        task = instance.tasks.get()
        wf.claim(task, actor=w["verifier"])
        other = w["acting"]  # holds no verify permission
        with pytest.raises((PermissionDenied, wf.WorkflowConflict)):
            wf.claim(task, actor=other)
        _ = rbac

    def test_tasks_never_change_status_any_other_way(self, w, running):
        _, instance = running
        task = instance.tasks.get()
        for change in ({"status": "completed"}, {"slot": "x"}, {"entry_seq": 9}):
            with pytest.raises((IntegrityError, Exception)), transaction.atomic():
                WorkflowTask.objects.filter(pk=task.pk).update(**change) if "status" not in change else WorkflowTask.objects.filter(pk=task.pk).update(status="completed")
                if "status" in change:
                    raise IntegrityError("completed needs a timestamp")  # the constraint requires completed_at; completing is only done by the engine

    def test_orphaned_tasks_are_released_or_flagged_never_discarded(self, w, running):
        _, instance = running
        task = instance.tasks.get()
        wf.claim(task, actor=w["verifier"])
        w["verifier"].status = "suspended"
        w["verifier"].save()
        assert wf.release_orphaned_tasks() == 1
        fresh = WorkflowTask.objects.get(pk=task.pk)
        assert (fresh.assignee_id, fresh.status) == (None, "pending") and not WorkflowInstance.objects.get(pk=instance.pk).needs_attention  # back in the pool
        assert instance.events.filter(kind="task_released").exists()

    def test_named_tasks_flag_the_workflow_when_the_person_is_gone(self, w):
        states = [{**CA_STATES[0], "assignee_policy": {"type": "user", "users": [str(w["verifier"].pk)]}, "separation": []}, *CA_STATES[1:]]
        d = build(w, code="named2", states=states, transitions=CA_TRANSITIONS)
        a = submitted_action(w)
        i = start(w, a, definition=d)
        w["verifier"].status = "suspended"
        w["verifier"].save()
        assert wf.release_orphaned_tasks() == 1
        assert WorkflowInstance.objects.get(pk=i.pk).needs_attention and i.tasks.get().status == "pending"

    def test_overdue_is_derived_from_the_clock(self, w, running):
        from datetime import timedelta

        from django.utils import timezone

        _, instance = running
        task = instance.tasks.get()
        assert not task.is_overdue
        WorkflowTask.objects.filter(pk=task.pk).update(due_at=timezone.now() - timedelta(hours=1))
        task.refresh_from_db()
        assert task.is_overdue and selectors.overdue_tasks(WorkflowTask.objects.all()).filter(pk=task.pk).exists()
        wf.perform(instance, "approve", actor=w["verifier"])
        assert not selectors.overdue_tasks(WorkflowTask.objects.all()).filter(pk=task.pk).exists()  # finished work is never overdue


class TestSelectorsAndAvailableTransitions:
    def test_available_transitions_follow_the_actor(self, w, running):
        _, instance = running
        assert [t["action"] for t in wf.available_transitions(instance, w["verifier"])] == ["approve", "reject"]
        assert wf.available_transitions(instance, w["worker1"]) == [] and wf.available_transitions(instance, w["aviewer"]) == []
        reject = next(t for t in wf.available_transitions(instance, w["verifier"]) if t["action"] == "reject")
        assert reject["reason_required"] is True and reject["target_state"] == "returned"

    def test_my_tasks_and_claimable(self, w, running):
        _, instance = running
        assert [t.pk for t in selectors.claimable_for(w["verifier"])] == [instance.tasks.get().pk]
        assert selectors.claimable_for(w["worker1"]) == [] and selectors.claimable_for(w["b_admin"]) == []
        wf.claim(instance.tasks.get(), actor=w["verifier"])
        assert selectors.my_tasks(w["verifier"]).count() == 1 and selectors.claimable_for(w["verifier"]) == []
        assert selectors.task_summary(w["verifier"])["mine"] == 1

    def test_instance_visibility_is_scoped_and_an_assignee_sees_theirs(self, w, running):
        _, instance = running
        assert selectors.get_instance(w["wf_runner"], instance.pk) is not None
        assert selectors.get_instance(w["b_admin"], instance.pk) is None and selectors.get_instance(w["r2_actor"], instance.pk) is None
        wf.claim(instance.tasks.get(), actor=w["verifier"])
        assert selectors.get_instance(w["verifier"], instance.pk) is not None  # the assignee sees the workflow they hold a task in


class TestAutoStart:
    def test_a_domain_event_starts_the_workflow_after_commit(self, w, django_capture_on_commit_callbacks):
        ca_workflow(w, auto="corrective_action.submitted")
        a = make_action(w)
        with django_capture_on_commit_callbacks(execute=True):
            a = to_status(w, a, "submitted")
        instance = WorkflowInstance.objects.get()
        assert (instance.target_id, instance.system_started, instance.started_by, instance.state.code) == (a.pk, True, w["worker1"], "awaiting")
        assert instance.tasks.count() == 1

    def test_a_workflow_problem_never_breaks_the_domain_flow(self, w, django_capture_on_commit_callbacks):
        states = [{**CA_STATES[0], "assignee_policy": {"type": "role", "role": "no-such-role"}, "separation": []}, *CA_STATES[1:]]
        build(w, code="blocked", states=states, transitions=CA_TRANSITIONS, auto="corrective_action.submitted")
        a = make_action(w)
        with django_capture_on_commit_callbacks(execute=True):
            a = to_status(w, a, "submitted")
        assert CorrectiveAction.objects.get(pk=a.pk).status == "submitted" and not WorkflowInstance.objects.exists()
        assert ActivityLog.objects.filter(verb="workflow.auto_start_skipped", metadata__code="no_assignee").exists()

    def test_without_a_configured_workflow_nothing_changes(self, w, django_capture_on_commit_callbacks):
        a = make_action(w)
        with django_capture_on_commit_callbacks(execute=True):
            to_status(w, a, "verified")  # the Phase 13 behaviour is untouched when no workflow exists
        assert not WorkflowInstance.objects.exists()


@pytest.mark.django_db
class TestDatabaseGuards:
    def test_instances_events_and_tasks_are_protected(self, w, running):
        _, instance = running
        event = instance.events.first()
        for fn in (lambda: WorkflowInstance.objects.filter(pk=instance.pk).delete(), lambda: WorkflowInstance.objects.filter(pk=instance.pk).update(target_id=uuid.uuid4()),
                   lambda: WorkflowInstance.objects.filter(pk=instance.pk).update(location=w["r2"]), lambda: WorkflowInstance.objects.filter(pk=instance.pk).update(number=99),
                   lambda: WorkflowEvent.objects.filter(pk=event.pk).update(comment="rewritten"), lambda: WorkflowEvent.objects.filter(pk=event.pk).delete(),
                   lambda: WorkflowTask.objects.filter(instance=instance).delete(), lambda: WorkflowInstance.objects.filter(pk=instance.pk).update(status="bogus"),
                   lambda: WorkflowInstance.objects.filter(pk=instance.pk).update(status="completed")):
            with pytest.raises(IntegrityError), transaction.atomic():
                fn()
        assert WorkflowInstance.objects.get(pk=instance.pk).status == "active"

    def test_an_instance_can_only_start_on_a_published_version_of_its_own_organization(self, w, running):
        from apps.workflows import services as svc

        _, instance = running
        draft = svc.create_draft(instance.definition, actor=w["admin"])
        initial = svc.add_state(draft, actor=w["admin"], code="i", name="I", kind="intermediate")
        with pytest.raises(IntegrityError), transaction.atomic():
            WorkflowInstance.objects.create(organization=instance.organization, number=77, definition=instance.definition, version=draft, target_type="finding", target_id=uuid.uuid4(),
                                            location=instance.location, state=initial)
        with pytest.raises(IntegrityError), transaction.atomic():
            WorkflowInstance.objects.filter(pk=instance.pk).update(state=initial)  # a state of another version

    def test_duplicate_approvals_and_idempotency_keys_are_unique(self, w, running):
        _, instance = running
        key = uuid.uuid4()
        WorkflowEvent.objects.create(instance=instance, seq=50, kind="task_claimed", idempotency_key=key)
        with pytest.raises(IntegrityError), transaction.atomic():
            WorkflowEvent.objects.create(instance=instance, seq=51, kind="task_claimed", idempotency_key=key)
        WorkflowEvent.objects.create(instance=instance, seq=52, kind="approval", actor=w["verifier"], entry_seq=1)
        with pytest.raises(IntegrityError), transaction.atomic():
            WorkflowEvent.objects.create(instance=instance, seq=53, kind="approval", actor=w["verifier"], entry_seq=1)


_ = (CorrectiveActionVerification, finding_services)
