"""Phase 13 services: creation, planning edits, assignment, the work log, the transition matrix, separation of duties, evidence requirement,
parent-finding policy, concurrency, database guards."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.core.models import ActivityLog
from apps.corrective_actions import eligibility, policies, selectors, services
from apps.corrective_actions.constants import TRANSITIONS, ActionStatus
from apps.corrective_actions.models import CorrectiveAction, CorrectiveActionUpdate, CorrectiveActionVerification
from apps.findings import services as finding_services
from apps.findings.models import Finding

from actions_support import build_actions_world, future, make_action, make_finding, to_status
from evidence_support import evidence_storage  # noqa: F401


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_actions_world(org_a, org_b, make_user, assign, make_type)


class TestCreation:
    def test_an_authorized_manager_creates_a_draft_linked_to_the_finding(self, w):
        f = make_finding(w)
        a = services.create_action(actor=w["manager"], finding=f, title="Repair it", description="Replace the seal", priority="high", assigned_to=w["worker1"],
                                   due_at=future(), plan="1. order 2. fit", expected_outcome="No leak", completion_criteria="Pressure test passes")
        assert (a.status, a.reference, a.finding_id, a.location_id, a.organization_id, a.created_by) == ("draft", "CA-00001", f.pk, f.location_id, f.organization_id, w["manager"])
        assert a.independent_verification is True and a.required_evidence == 0 and a.issued_at is None
        assert ActivityLog.objects.filter(verb="corrective_action.created", metadata__finding_id=str(f.pk), actor=w["manager"]).exists()

    def test_many_actions_belong_to_one_finding_independently(self, w):
        f = make_finding(w)
        a, b = make_action(w, f, title="Repair"), make_action(w, f, title="Update the procedure", owner="worker2")
        assert f.corrective_actions.count() == 2 and a.assigned_to != b.assigned_to
        to_status(w, a, "in_progress")
        assert CorrectiveAction.objects.get(pk=b.pk).status == "open"  # independent lifecycles

    def test_creating_requires_the_permission_a_visible_finding_and_an_open_finding(self, w):
        f = make_finding(w)
        with pytest.raises(PermissionDenied):
            services.create_action(actor=w["viewer"], finding=f, title="x")  # can see the finding, may not add actions
        with pytest.raises(services.ActionError) as exc:  # a finding the actor cannot see is "unknown"
            services.create_action(actor=w["r2_actor"], finding=f, title="x")
        assert exc.value.action_code == "unknown_finding"
        finding_services.transition(f, "cancel", actor=w["admin"], reason="Error")
        with pytest.raises(services.ActionConflict) as closed:
            services.create_action(actor=w["manager"], finding=f, title="x")
        assert closed.value.action_code == "finding_not_open"

    def test_field_validation(self, w):
        f = make_finding(w)
        for kw, code in (({"title": " "}, "required_field"), ({"title": "x" * 201}, "too_long"), ({"due_at": timezone.now() - timedelta(hours=1)}, "invalid_due"),
                         ({"priority": "urgent"}, "invalid_priority"), ({"required_evidence": 11}, "invalid_evidence_requirement"),
                         ({"plan": "p" * 2001}, "too_long")):
            with pytest.raises(services.ActionError) as exc:
                services.create_action(actor=w["manager"], finding=f, **{"title": "ok", **kw})
            assert exc.value.action_code == code

    def test_the_owner_must_be_eligible(self, w):
        f = make_finding(w)
        for who in ("aviewer", "verifier", "r2_actor", "b_admin"):
            with pytest.raises(services.ActionError) as exc:
                services.create_action(actor=w["manager"], finding=f, title="x", assigned_to=w[who])
            assert exc.value.action_code == "invalid_assignee"
        w["worker1"].status = "suspended"
        w["worker1"].save()
        with pytest.raises(services.ActionError):
            services.create_action(actor=w["manager"], finding=f, title="x", assigned_to=w["worker1"])

    def test_issuing_needs_an_owner_all_or_nothing(self, w):
        f = make_finding(w)
        with pytest.raises(services.ActionError) as exc:
            services.create_action(actor=w["manager"], finding=f, title="x", issue=True)
        assert exc.value.action_code == "owner_required" and not CorrectiveAction.objects.exists()  # no half-created draft is left behind

    def test_a_creator_who_may_carry_out_actions_can_assign_themselves(self, w, make_user, assign):
        from apps.permissions import services as rbac
        from apps.permissions.models import MembershipRole
        from apps.permissions.registry import P

        role = rbac.create_role(organization=w["org_a"], name="Creator and doer", permissions=[
            P.CORRECTIVE_ACTION_VIEW, P.CORRECTIVE_ACTION_CREATE, P.CORRECTIVE_ACTION_START, P.FINDING_VIEW, P.LOCATION_VIEW])
        doer = make_user(email="ca-doer@example.com", organization=w["org_a"], role=None)
        assign(doer, w["org_a"], role, scope=MembershipRole.ScopeType.LOCATION, location=w["r1"])
        a = services.create_action(actor=doer, finding=make_finding(w), title="Mine", assigned_to=doer, issue=True)
        assert a.assigned_to == doer and a.created_by == doer and a.status == "open"

    def test_numbers_are_sequential_per_organization(self, w):
        f = make_finding(w)
        assert [make_action(w, f, issue=False).number for _ in range(3)] == [1, 2, 3]


class TestPlanAndAssignment:
    def test_the_manager_edits_everything_the_owner_only_the_plan(self, w):
        a = make_action(w)
        services.update_action(a, actor=w["manager"], title="Repair and test", priority="critical", due_at=future(5), required_evidence=2)
        a.refresh_from_db()
        assert (a.title, a.priority, a.required_evidence) == ("Repair and test", "critical", 2)
        for verb in ("corrective_action.updated", "corrective_action.due_date_changed", "corrective_action.priority_changed"):
            assert ActivityLog.objects.filter(verb=verb, metadata__action_id=str(a.pk)).exists()
        services.update_action(a, actor=w["worker1"], plan="Order the seal, fit it.")
        with pytest.raises(PermissionDenied):
            services.update_action(a, actor=w["worker1"], due_at=future(9))  # the owner cannot move their own deadline
        with pytest.raises(PermissionDenied):
            services.update_action(a, actor=w["worker1"], priority="low")
        with pytest.raises(PermissionDenied):
            services.update_action(a, actor=w["worker2"], plan="x")
        with pytest.raises(services.ActionError) as exc:
            services.update_action(a, actor=w["manager"], status="verified")
        assert exc.value.action_code == "protected_field"

    def test_submitted_and_finished_actions_cannot_be_edited(self, w):
        a = to_status(w, make_action(w), "submitted")
        for who, kw in (("manager", {"title": "x"}), ("worker1", {"plan": "x"})):
            with pytest.raises(services.ActionConflict) as exc:
                services.update_action(a, actor=w[who], **kw)
            assert exc.value.action_code == "not_editable"

    def test_an_unchanged_edit_is_a_no_op(self, w):
        a = make_action(w)
        before = ActivityLog.objects.count()
        services.update_action(a, actor=w["manager"], title=a.title)
        assert ActivityLog.objects.count() == before

    def test_assign_and_reassign_follow_their_permissions_and_are_audited(self, w):
        a = make_action(w, owner=None, issue=False)
        services.assign(a, actor=w["manager"], assigned_to=w["worker1"])
        a.refresh_from_db()
        assert a.assigned_to == w["worker1"]
        services.assign(a, actor=w["manager"], assigned_to=w["worker2"])  # the manager holds reassign
        entry = ActivityLog.objects.filter(verb="corrective_action.assigned", metadata__action_id=str(a.pk)).order_by("-created_at").first()
        assert entry.metadata["changes"]["assigned_to"]["to"] == str(w["worker2"].pk)
        with pytest.raises(PermissionDenied):
            services.assign(a, actor=w["worker2"], assigned_to=w["worker1"])  # the owner cannot hand their work away
        with pytest.raises(services.ActionError):
            services.assign(a, actor=w["manager"], assigned_to=w["aviewer"])

    def test_work_in_progress_cannot_be_left_without_an_owner(self, w):
        a = to_status(w, make_action(w), "in_progress")
        with pytest.raises(services.ActionError) as exc:
            services.assign(a, actor=w["manager"], assigned_to=None)
        assert exc.value.action_code == "owner_required"
        services.assign(a, actor=w["manager"], assigned_to=w["worker2"])  # reassigning mid-work is allowed and keeps the status
        assert CorrectiveAction.objects.get(pk=a.pk).status == "in_progress"


class TestWorkLog:
    def test_progress_notes_are_append_only_and_never_change_the_status(self, w):
        a = to_status(w, make_action(w), "in_progress")
        u = services.add_update(a, actor=w["worker1"], body="Part ordered.", progress_percent=100)  # 100% is information, not verification
        assert (u.kind, u.progress_percent, u.author) == ("progress", 100, w["worker1"])
        assert CorrectiveAction.objects.get(pk=a.pk).status == "in_progress"
        fix = services.add_update(a, actor=w["worker1"], body="Typo: the part was ordered on Monday.", kind="correction", corrects=u)
        assert fix.corrects_id == u.pk and CorrectiveActionUpdate.objects.get(pk=u.pk).body == "Part ordered."  # the original is untouched

    def test_validation_and_authorization(self, w):
        a = to_status(w, make_action(w), "in_progress")
        for kw, code in (({"body": " "}, "required_field"), ({"body": "x", "progress_percent": 101}, "invalid_progress"), ({"body": "x", "progress_percent": True}, "invalid_progress"),
                         ({"body": "x", "kind": "correction"}, "invalid_correction"), ({"body": "x", "kind": "submission"}, "invalid_kind")):
            with pytest.raises(services.ActionError) as exc:
                services.add_update(a, actor=w["worker1"], **kw)
            assert exc.value.action_code == code
        for who in ("worker2", "aviewer", "verifier"):
            with pytest.raises(PermissionDenied):
                services.add_update(a, actor=w[who], body="x")

    def test_updates_are_only_possible_while_work_is_under_way(self, w):
        a = make_action(w)  # open, not started
        with pytest.raises(services.ActionConflict):
            services.add_update(a, actor=w["worker1"], body="x")
        done = to_status(w, make_action(w, title="second"), "submitted")
        with pytest.raises(services.ActionConflict):
            services.add_update(done, actor=w["worker1"], body="x")

    def test_blockers_and_resubmission_entries_are_logged_in_order(self, w):
        a = to_status(w, make_action(w), "blocked")
        assert a.blocker == "Waiting for a part" and a.blocked_at
        a = services.transition(a, "resume", actor=w["worker1"], reason="Part arrived")
        assert a.blocker == "" and a.blocked_at is None
        kinds = list(a.updates.order_by("created_at").values_list("kind", flat=True))
        assert kinds == ["blocker", "blocker_cleared"]


class TestTransitionMatrix:
    def test_the_documented_happy_path(self, w):
        a = make_action(w, issue=False)
        a = services.transition(a, "issue", actor=w["manager"])
        assert (a.status, a.issued_by) == ("open", w["manager"])
        a = services.transition(a, "start", actor=w["worker1"])
        assert a.started_at
        a = services.transition(a, "submit", actor=w["worker1"], reason="All done.")
        assert (a.status, a.submission_count, a.submitted_by, a.completion_summary) == ("submitted", 1, w["worker1"], "All done.")
        a = services.transition(a, "verify", actor=w["verifier"], reason="Checked.")
        assert (a.status, a.verified_by) == ("verified", w["verifier"])
        v = CorrectiveActionVerification.objects.get(action=a)
        assert (v.decision, v.verifier, v.submission_number, v.notes) == ("approved", w["verifier"], 1, "Checked.")
        a = services.transition(a, "close", actor=w["manager"])
        assert (a.status, a.closed_by) == ("closed", w["manager"])
        verbs = [e.verb for e in ActivityLog.objects.filter(metadata__action_id=str(a.pk)).order_by("created_at")]
        assert verbs[0] == "corrective_action.created" and verbs[1:] == ["corrective_action.issued", "corrective_action.started", "corrective_action.submitted",
                                                                          "corrective_action.verified", "corrective_action.closed"]

    def test_every_invalid_move_is_refused_and_changes_nothing(self, w):
        for name, spec in TRANSITIONS.items():
            for status in ActionStatus.values:
                if status in spec.sources or status == spec.target:
                    continue
                f = make_finding(w, title=f"{name} from {status}")
                a = make_action(w, f)
                CorrectiveAction.objects.filter(pk=a.pk).update(**_state(status))
                with pytest.raises(services.ActionError) as exc:
                    services.transition(a, name, actor=w["admin"], reason="because")
                assert exc.value.action_code == "invalid_transition", (name, status)
                assert CorrectiveAction.objects.get(pk=a.pk).status == status

    def test_a_repeat_is_a_conflict_not_a_second_change(self, w):
        a = make_action(w)
        services.transition(a, "start", actor=w["worker1"])
        with pytest.raises(services.ActionConflict) as exc:
            services.transition(a, "start", actor=w["worker1"])
        assert exc.value.action_code == "already_in_progress"
        assert ActivityLog.objects.filter(verb="corrective_action.started", metadata__action_id=str(a.pk)).count() == 1

    def test_ownership_and_permissions_per_action(self, w):
        a = make_action(w)
        for who, name in (("worker2", "start"), ("aviewer", "start"), ("verifier", "start"), ("worker1", "close"), ("worker1", "cancel"), ("worker1", "verify")):
            with pytest.raises(PermissionDenied):
                services.transition(a, name, actor=w[who], reason="x")
        with pytest.raises(PermissionDenied):
            services.transition(a, "start", actor=w["r2_actor"])  # out of scope
        services.transition(a, "start", actor=w["worker1"])

    def test_reasons_are_required_where_the_matrix_says(self, w):
        a = to_status(w, make_action(w), "in_progress")
        for name, who in (("block", "worker1"), ("submit", "worker1"), ("cancel", "manager")):
            with pytest.raises(services.ActionError) as exc:
                services.transition(a, name, actor=w[who], reason=" ")
            assert exc.value.action_code == "reason_required", name
        with pytest.raises(services.ActionError) as exc:
            services.transition(a, "cancel", actor=w["manager"], reason="x" * 301)
        assert exc.value.action_code == "too_long"
        s = to_status(w, make_action(w, title="second"), "submitted")
        with pytest.raises(services.ActionError) as exc:
            services.transition(s, "reject", actor=w["verifier"], reason="")
        assert exc.value.action_code == "reason_required"
        assert not CorrectiveActionVerification.objects.filter(action=s).exists()

    def test_cancelled_is_final_and_a_cancelled_action_cannot_continue(self, w):
        a = make_action(w)
        services.transition(a, "cancel", actor=w["manager"], reason="No longer needed")
        for name in ("start", "submit", "verify", "close", "resume", "reopen", "block"):
            with pytest.raises(services.ActionError):
                services.transition(a, name, actor=w["admin"], reason="x")

    def test_the_owner_is_required_to_start_and_submit(self, w):
        a = make_action(w, owner=None, issue=False)
        CorrectiveAction.objects.filter(pk=a.pk).update(status="open", issued_at=timezone.now())
        with pytest.raises(services.ActionError) as exc:
            services.transition(a, "start", actor=w["acting"])  # a manager may act for an owner - but there is none
        assert exc.value.action_code == "owner_required"

    def test_a_manager_holding_the_permission_may_act_for_the_owner(self, w):
        a = make_action(w)
        assert not policies.can_act(w["manager"], a, "start")  # holding ``update`` alone is not enough: the specific permission is still required
        assert policies.can_act(w["acting"], a, "start")
        assert services.transition(a, "start", actor=w["acting"]).status == "in_progress"
        assert services.transition(a, "submit", actor=w["acting"], reason="Done on behalf of the owner").submitted_by == w["acting"]


class TestRejectionAndResubmission:
    def test_rejection_returns_the_action_to_its_owner_for_correction(self, w):
        a = to_status(w, make_action(w), "rejected")
        v = CorrectiveActionVerification.objects.get(action=a)
        assert (a.status, v.decision, v.reason) == ("rejected", "rejected", "Not tested under load.") and a.verified_at is None
        a = services.transition(a, "resume", actor=w["worker1"])
        assert a.status == "in_progress"
        a = services.transition(a, "submit", actor=w["worker1"], reason="Retested under load.")
        assert a.submission_count == 2
        a = services.transition(a, "verify", actor=w["verifier"])
        decisions = list(CorrectiveActionVerification.objects.filter(action=a).order_by("submission_number").values_list("submission_number", "decision"))
        assert decisions == [(1, "rejected"), (2, "approved")]  # every decision is preserved

    def test_a_rejected_action_can_only_be_resumed_by_its_owner(self, w):
        a = to_status(w, make_action(w), "rejected")
        with pytest.raises(PermissionDenied):
            services.transition(a, "resume", actor=w["worker2"])

    def test_verification_needs_a_submission(self, w):
        a = to_status(w, make_action(w), "in_progress")
        with pytest.raises(services.ActionError) as exc:
            services.transition(a, "verify", actor=w["verifier"])
        assert exc.value.action_code == "invalid_transition" and not CorrectiveActionVerification.objects.exists()


class TestSeparationOfDuties:
    def test_the_owner_and_the_submitter_cannot_verify_their_own_work(self, w):
        a = make_action(w, owner="both")
        services.transition(a, "start", actor=w["both"])
        a = services.transition(a, "submit", actor=w["both"], reason="Done")
        for name in ("verify", "reject"):
            with pytest.raises(PermissionDenied):
                services.transition(a, name, actor=w["both"], reason="I approve my own work")
        assert not CorrectiveActionVerification.objects.exists() and CorrectiveAction.objects.get(pk=a.pk).status == "submitted"
        assert services.transition(a, "verify", actor=w["verifier"]).status == "verified"

    def test_a_manager_who_submitted_for_the_owner_cannot_verify_it_either(self, w):
        a = make_action(w, owner="worker1")
        services.transition(a, "start", actor=w["worker1"])
        a = CorrectiveAction.objects.get(pk=a.pk)
        a = services.transition(a, "submit", actor=w["worker1"], reason="Done")
        CorrectiveAction.objects.filter(pk=a.pk).update(submitted_by=w["both"])
        a.refresh_from_db()
        assert not policies.separation_ok(w["both"], a) and policies.separation_ok(w["verifier"], a)

    def test_the_creator_restriction_follows_the_setting(self, w, settings):
        settings.CORRECTIVE_ACTION_VERIFIER_NOT_CREATOR = True
        f = make_finding(w)
        a = make_action(w, f)
        a = to_status(w, a, "submitted")
        CorrectiveAction.objects.filter(pk=a.pk).update(created_by=w["verifier"])
        a.refresh_from_db()
        assert not policies.separation_ok(w["verifier"], a)
        assert not selectors.verification_queue(w["verifier"]).filter(pk=a.pk).exists()

    def test_independence_can_be_switched_off_per_deployment(self, w, settings):
        settings.CORRECTIVE_ACTION_INDEPENDENT_VERIFICATION = False
        a = make_action(w, owner="both")
        assert a.independent_verification is False
        services.transition(a, "start", actor=w["both"])
        a = services.transition(a, "submit", actor=w["both"], reason="Done")
        assert services.transition(a, "verify", actor=w["both"]).status == "verified"

    def test_the_policy_is_snapshotted_so_a_later_setting_change_does_not_alter_running_actions(self, w, settings):
        a = make_action(w)
        settings.CORRECTIVE_ACTION_INDEPENDENT_VERIFICATION = False
        assert CorrectiveAction.objects.get(pk=a.pk).independent_verification is True


class TestEvidenceRequirement:
    def test_submission_needs_the_required_number_of_verified_completion_files(self, w):
        from apps.evidence import services as ev

        from evidence_support import jpeg_bytes

        a = make_action(w, required_evidence=2)
        a = to_status(w, a, "in_progress")
        with pytest.raises(services.ActionError) as exc:
            services.transition(a, "submit", actor=w["worker1"], reason="Done")
        assert exc.value.action_code == "evidence_required"
        for i in range(2):
            ev.upload(w["worker1"], target_type="corrective_action", ref={"corrective_action_id": str(a.pk), "purpose": "completion"}, filename=f"p{i}.jpg",
                      data=jpeg_bytes(size=(60 + i, 50)))
        assert selectors.verified_completion_count(a) == 2
        assert services.transition(a, "submit", actor=w["worker1"], reason="Done").status == "submitted"


class TestFindingPolicy:
    def test_creating_or_verifying_an_action_never_resolves_or_closes_the_finding(self, w):
        f = make_finding(w)
        a = make_action(w, f)
        assert Finding.objects.get(pk=f.pk).status == "open"
        to_status(w, a, "verified")
        assert Finding.objects.get(pk=f.pk).status == "open"
        services.transition(CorrectiveAction.objects.get(pk=a.pk), "close", actor=w["manager"])
        assert Finding.objects.get(pk=f.pk).status == "open"

    def test_a_finding_cannot_be_resolved_while_actions_are_outstanding(self, w):
        f = make_finding(w)
        a1, a2 = make_action(w, f, title="Repair"), make_action(w, f, title="Train staff", owner="worker2")
        with pytest.raises(finding_services.FindingError) as exc:
            finding_services.transition(f, "resolve", actor=w["admin"], reason="Fixed")
        assert exc.value.finding_code == "resolution_blocked" and "CA-00001" in str(exc.value.messages[0])
        to_status(w, a1, "verified")
        with pytest.raises(finding_services.FindingError):  # one verified action does not free a finding with another outstanding one
            finding_services.transition(f, "resolve", actor=w["admin"], reason="Fixed")
        services.transition(a2, "cancel", actor=w["manager"], reason="Not needed")  # cancelling an action does not silently change the finding
        assert Finding.objects.get(pk=f.pk).status == "open"
        assert finding_services.transition(f, "resolve", actor=w["admin"], reason="All done").status == "resolved"

    def test_eligibility_facts(self, w):
        f = make_finding(w)
        assert eligibility.finding_eligibility(f).eligible
        a = make_action(w, f)
        e = eligibility.finding_eligibility(f)
        assert not e.eligible and e.outstanding == ["CA-00001"]
        to_status(w, a, "verified")
        e = eligibility.finding_eligibility(f)
        assert e.eligible and e.done == 1 and e.as_dict()["outstanding"] == []

    def test_the_policy_can_be_switched_off(self, w, settings):
        settings.CORRECTIVE_ACTIONS_BLOCK_FINDING_RESOLUTION = False
        f = make_finding(w)
        make_action(w, f)
        assert finding_services.transition(f, "resolve", actor=w["admin"], reason="Resolved anyway").status == "resolved"

    def test_a_finding_with_live_actions_cannot_be_cancelled(self, w):
        f = make_finding(w)
        a = make_action(w, f)
        with pytest.raises(finding_services.FindingError) as exc:
            finding_services.transition(f, "cancel", actor=w["admin"], reason="Error")
        assert exc.value.finding_code == "cancellation_blocked"
        services.transition(a, "cancel", actor=w["manager"], reason="Raised in error")
        assert finding_services.transition(f, "cancel", actor=w["admin"], reason="Error").status == "cancelled"

    def test_the_verification_gate_gives_finding_closure_a_real_meaning(self, w, settings):
        settings.FINDINGS_REQUIRE_VERIFICATION = True
        f = make_finding(w)
        a = make_action(w, f)
        a = to_status(w, a, "verified")
        f2 = make_finding(w, title="Other")
        finding_services.transition(f2, "resolve", actor=w["admin"], reason="Done")
        with pytest.raises(finding_services.FindingError) as exc:  # no verified action: closure is refused rather than faked
            finding_services.transition(f2, "close", actor=w["admin"])
        assert exc.value.finding_code == "closure_blocked"
        finding_services.transition(f, "resolve", actor=w["admin"], reason="Done")
        assert finding_services.transition(f, "close", actor=w["admin"]).status == "closed"

    def test_reopening_an_action_does_not_touch_the_finding(self, w):
        f = make_finding(w)
        a = to_status(w, make_action(w, f), "verified")
        finding_services.transition(f, "resolve", actor=w["admin"], reason="Done")
        a = services.transition(a, "reopen", actor=w["manager"], reason="The leak is back")
        assert a.status == "in_progress" and a.reopen_count == 1 and a.verified_at is None
        assert Finding.objects.get(pk=f.pk).status == "resolved"  # the finding is only changed by a person, via the finding service
        assert CorrectiveActionVerification.objects.filter(action=a).count() == 1  # the earlier verification stays in history


class TestOverdue:
    def test_overdue_is_derived_from_the_clock_and_the_status(self, w):
        a = make_action(w, due_at=future(1))
        CorrectiveAction.objects.filter(pk=a.pk).update(due_at=timezone.now() - timedelta(days=1))
        a.refresh_from_db()
        assert a.is_overdue and a.effective_status == "overdue" and a.status == "open"
        assert selectors.overdue(CorrectiveAction.objects.all()).filter(pk=a.pk).exists()
        for status, expected in (("in_progress", True), ("blocked", True), ("rejected", True), ("submitted", False), ("verified", False), ("closed", False), ("cancelled", False)):
            CorrectiveAction.objects.filter(pk=a.pk).update(**_state(status))
            a.refresh_from_db()
            assert a.is_overdue is expected, status
            assert selectors.overdue(CorrectiveAction.objects.all()).filter(pk=a.pk).exists() is expected
        CorrectiveAction.objects.filter(pk=a.pk).update(status="open", due_at=None, cancelled_at=None, cancel_reason="", verified_at=None, closed_at=None)
        a.refresh_from_db()
        assert not a.is_overdue  # no due date: never overdue, none is invented


class TestConcurrency:
    @pytest.mark.django_db(transaction=True)
    def test_concurrent_decisions_on_one_submission_decide_once(self, w):
        a = to_status(w, make_action(w), "submitted")
        outcomes = []

        def run(name):
            try:
                services.transition(a, name, actor=w["verifier"], reason="because")
                outcomes.append(name)
            except services.ActionError as exc:
                outcomes.append(exc.action_code)
            except Exception as exc:  # noqa: BLE001
                outcomes.append(repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=run, args=(n,)) for n in ("verify", "verify", "reject")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len([o for o in outcomes if o in ("verify", "reject")]) == 1, outcomes
        assert CorrectiveActionVerification.objects.filter(action=a).count() == 1

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_submissions_create_one_submission(self, w):
        a = to_status(w, make_action(w), "in_progress")
        results = []

        def run():
            try:
                services.transition(a, "submit", actor=w["worker1"], reason="Done")
                results.append("ok")
            except services.ActionConflict:
                results.append("conflict")
            except Exception as exc:  # noqa: BLE001
                results.append(repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(results) == ["conflict", "conflict", "ok"]
        assert CorrectiveAction.objects.get(pk=a.pk).submission_count == 1

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_creation_numbers_are_unique(self, w):
        f = make_finding(w)
        numbers, errors = [], []

        def run():
            try:
                numbers.append(make_action(w, f, issue=False).number)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors and sorted(numbers) == [1, 2, 3, 4]


@pytest.mark.django_db
class TestDatabaseGuards:
    def test_actions_are_never_deleted_and_identity_is_immutable(self, w):
        a = make_action(w)
        with pytest.raises(IntegrityError), transaction.atomic():
            CorrectiveAction.objects.filter(pk=a.pk).delete()
        other = make_finding(w, title="Another")
        for change in ({"finding": other}, {"number": 99}, {"location": w["r2"]}):
            with pytest.raises(IntegrityError), transaction.atomic():
                CorrectiveAction.objects.filter(pk=a.pk).update(**change)

    def test_organization_and_location_must_be_the_findings(self, w, org_b):
        f = make_finding(w)
        with pytest.raises(IntegrityError), transaction.atomic():
            CorrectiveAction.objects.create(organization=org_b, number=1, finding=f, location=f.location, title="x")
        with pytest.raises(IntegrityError), transaction.atomic():
            CorrectiveAction.objects.create(organization=f.organization, number=2, finding=f, location=w["r2"], title="x")

    def test_history_tables_are_append_only(self, w):
        a = to_status(w, make_action(w), "verified")
        u = a.updates.first()
        v = a.verifications.first()
        for model, row in ((CorrectiveActionUpdate, u), (CorrectiveActionVerification, v)):
            with pytest.raises(IntegrityError), transaction.atomic():
                model.objects.filter(pk=row.pk).update(**({"body": "rewritten"} if model is CorrectiveActionUpdate else {"decision": "rejected", "reason": "x"}))
            with pytest.raises(IntegrityError), transaction.atomic():
                model.objects.filter(pk=row.pk).delete()
        assert CorrectiveActionVerification.objects.get(pk=v.pk).decision == "approved"

    def test_a_verification_cannot_be_inserted_without_a_submission(self, w):
        a = to_status(w, make_action(w), "in_progress")
        with pytest.raises(IntegrityError), transaction.atomic():
            CorrectiveActionVerification.objects.create(action=a, submission_number=1, decision="approved", verifier=w["verifier"])

    def test_constraints_protect_the_invariants(self, w):
        a = make_action(w)
        for change in ({"status": "bogus"}, {"status": "blocked", "blocker": ""}, {"status": "submitted", "submitted_at": timezone.now()},
                       {"status": "closed", "closed_at": timezone.now()}, {"status": "cancelled", "cancelled_at": timezone.now(), "cancel_reason": ""},
                       {"priority": "urgent"}, {"required_evidence": 11}, {"status": "verified"}):
            with pytest.raises(IntegrityError), transaction.atomic():
                CorrectiveAction.objects.filter(pk=a.pk).update(**change)
        with pytest.raises(IntegrityError), transaction.atomic():
            CorrectiveActionVerification.objects.create(action=to_status(w, make_action(w, title="two"), "submitted"), submission_number=1, decision="rejected", verifier=w["verifier"])  # no reason
        with pytest.raises(IntegrityError), transaction.atomic():
            CorrectiveActionUpdate.objects.create(action=a, kind="progress", body="x", progress_percent=101)

    def test_a_used_finding_cannot_be_deleted(self, w):
        from django.db.models import ProtectedError

        a = make_action(w)
        with pytest.raises(ProtectedError):
            a.finding.delete()


def _state(status):
    now = timezone.now()
    return {
        "draft": {"status": "draft", "issued_at": None}, "open": {"status": "open", "issued_at": now}, "in_progress": {"status": "in_progress", "issued_at": now},
        "blocked": {"status": "blocked", "blocker": "x", "issued_at": now}, "rejected": {"status": "rejected", "issued_at": now},
        "submitted": {"status": "submitted", "issued_at": now, "submitted_at": now, "submission_count": 1, "completion_summary": "x"},
        "verified": {"status": "verified", "issued_at": now, "verified_at": now}, "closed": {"status": "closed", "issued_at": now, "verified_at": now, "closed_at": now},
        "cancelled": {"status": "cancelled", "issued_at": now, "cancelled_at": now, "cancel_reason": "x"},
    }[status]
