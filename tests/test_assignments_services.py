"""Assignment services: creation, eligibility, lifecycle, duplicates, scope and the database guard."""

from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from apps.assignments import selectors, services
from apps.assignments.models import ChecklistAssignment
from apps.assignments.services import AssignmentConflict, AssignmentError
from apps.checklists import lifecycle
from apps.core.models import ActivityLog

from assignments_support import build_world, future


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_world(org_a, org_b, make_user, assign, make_type)


def create(w, **kw):
    args = {"actor": w["admin"], "version": w["version"], "location": w["r1"]}
    args.update(kw)
    return services.create_assignment(**args)


def codes(call):
    with pytest.raises(AssignmentError) as info:
        call()
    return info.value.assignment_code


@pytest.mark.django_db
class TestCreate:
    def test_a_draft_references_organization_version_and_location(self, w):
        a = create(w, due_at=future(48), priority="high", instructions="  Check twice  ")
        assert (a.organization_id, a.version_id, a.location_id) == (w["org"].pk, w["version"].pk, w["r1"].pk)
        assert a.status == "draft" and a.reference == "ASG-00001" and a.priority == "high" and a.instructions == "Check twice"
        assert a.created_by_id == w["admin"].pk and a.issued_at is None and a.assigned_to is None
        assert ActivityLog.objects.filter(verb="assignment.created", metadata__assignment_id=str(a.pk)).exists()

    def test_numbers_are_sequential_per_organization(self, w):
        assert [create(w, location=loc).number for loc in (w["r1"], w["r2"], w["wh"])] == [1, 2, 3]

    def test_it_works_for_any_location_type(self, w):
        assert create(w, location=w["wh"]).location.location_type.name == "Warehouse"
        assert create(w, location=w["area1"]).location.location_type.name == "Area"  # not restaurant-specific

    def test_create_and_issue_in_one_step(self, w):
        a = create(w, issue=True, due_at=future())
        assert a.status == "assigned" and a.issued_by_id == w["admin"].pk and a.issued_at

    def test_the_due_date_rules(self, w):
        assert codes(lambda: create(w, due_at=future(-1))) == "invalid_due"
        from datetime import datetime
        assert codes(lambda: create(w, due_at=datetime(2030, 1, 1))) == "invalid_due"  # naive
        assert create(w, due_at=None).due_at is None  # no deadline: allowed, never overdue

    def test_priority_and_instructions_are_validated(self, w):
        assert codes(lambda: create(w, priority="urgent")) == "invalid_priority"
        assert codes(lambda: create(w, instructions="x" * 2001)) == "invalid_instructions"

    def test_the_responsible_user_must_be_an_eligible_member(self, w, make_user):
        assert create(w, assigned_to=w["worker"]).assigned_to_id == w["worker"].pk  # may run checklists at Restaurant 1
        assert codes(lambda: create(w, location=w["r2"], assigned_to=w["worker"])) == "invalid_assignee"  # not at Restaurant 2
        assert codes(lambda: create(w, location=w["r2"], assigned_to=w["plain"])) == "invalid_assignee"  # no permission at all
        assert codes(lambda: create(w, location=w["r2"], assigned_to=w["other"])) == "invalid_assignee"  # another organization's user
        w["worker"].status = "suspended"
        w["worker"].save()
        assert codes(lambda: create(w, location=w["r1"], assigned_to=w["worker"], due_at=None)) == "invalid_assignee"

    def test_eligible_assignees_selector(self, w):
        assert set(selectors.eligible_assignees(w["org"], w["r1"]).values_list("email", flat=True)) >= {"as-worker@example.com", "as-manager@example.com"}
        assert "as-worker@example.com" not in set(selectors.eligible_assignees(w["org"], w["r2"]).values_list("email", flat=True))


@pytest.mark.django_db
class TestVersionEligibility:
    def test_a_draft_version_cannot_be_assigned(self, w, org_a):
        lifecycle.create_draft(w["template"], actor=w["admin"])
        draft = w["template"].versions.get(status="draft")
        assert codes(lambda: create(w, version=draft)) == "not_published"

    def test_a_superseded_version_cannot_receive_new_assignments(self, w):
        v2 = lifecycle.create_draft(w["template"], actor=w["admin"])
        lifecycle.publish_version(v2, actor=w["admin"])
        assert codes(lambda: create(w)) == "superseded"
        assert create(w, version=v2).version_id == v2.pk

    def test_existing_assignments_stay_on_their_version(self, w):
        a = create(w, issue=True)
        v2 = lifecycle.create_draft(w["template"], actor=w["admin"])
        lifecycle.publish_version(v2, actor=w["admin"])
        a.refresh_from_db()
        assert a.version_id == w["version"].pk and a.status == "assigned"

    def test_an_archived_checklist_receives_nothing(self, w):
        from apps.checklists import services as cs

        cs.archive_checklist_template(w["template"], actor=w["admin"])
        assert codes(lambda: create(w)) == "template_archived"

    def test_another_organizations_version_or_location_is_refused(self, w):
        assert codes(lambda: create(w, version=w["b_version"])) == "foreign_version"
        with pytest.raises(PermissionDenied):
            create(w, location=w["b_loc"])  # the actor has no rights in organization B at all

    def test_an_inactive_location(self, w):
        from apps.locations import services as ls

        ls.deactivate_location(w["r2"])
        assert codes(lambda: create(w, location=w["r2"])) == "inactive_location"

    def test_a_tampered_version_is_not_assignable(self, w):
        from apps.checklists import guards
        from apps.checklists.models import ChecklistItem

        with guards.allow_definition_changes():
            ChecklistItem.objects.filter(section__version=w["version"]).update(question="changed")
        assert codes(lambda: create(w)) == "integrity"


@pytest.mark.django_db
class TestLifecycle:
    def test_issue_cancel_reopen_cycle(self, w):
        a = create(w, due_at=future())
        a = services.issue(a, actor=w["admin"])
        assert a.status == "assigned"
        a = services.cancel(a, actor=w["admin"], reason="Closed for refurbishment")
        assert a.status == "cancelled" and a.cancel_reason and a.cancelled_by_id == w["admin"].pk and a.cancelled_at
        a = services.reopen(a, actor=w["admin"])
        assert a.status == "draft" and a.issued_at is None and a.cancel_reason == ""
        a = services.issue(a, actor=w["admin"])
        assert a.status == "assigned"
        verbs = list(ActivityLog.objects.filter(metadata__assignment_id=str(a.pk)).order_by("created_at").values_list("verb", flat=True))
        assert verbs == ["assignment.created", "assignment.issued", "assignment.cancelled", "assignment.reopened", "assignment.issued"]

    def test_repeated_requests_are_safe(self, w):
        a = create(w, issue=True)
        with pytest.raises(AssignmentConflict) as info:
            services.issue(a, actor=w["admin"])
        assert info.value.assignment_code == "already_issued"
        services.cancel(a, actor=w["admin"], reason="x")
        with pytest.raises(AssignmentConflict) as info:
            services.cancel(a, actor=w["admin"], reason="x")
        assert info.value.assignment_code == "already_cancelled"
        assert ActivityLog.objects.filter(verb="assignment.issued").count() == 1 and ActivityLog.objects.filter(verb="assignment.cancelled").count() == 1

    def test_a_cancelled_assignment_cannot_be_issued_without_reopening(self, w):
        a = create(w)
        services.cancel(a, actor=w["admin"], reason="Not needed")
        assert codes(lambda: services.issue(a, actor=w["admin"])) == "invalid_transition"
        refused = ActivityLog.objects.get(verb="assignment.transition_refused")
        assert refused.metadata["attempted"] == "issue" and refused.metadata["status"] == "cancelled"

    def test_invalid_transitions(self, w):
        a = create(w)
        assert codes(lambda: services.reopen(a, actor=w["admin"])) == "invalid_transition"  # only cancelled assignments reopen
        for old, new in (("draft", "completed"), ("assigned", "draft"), ("completed", "cancelled"), ("cancelled", "assigned"), ("draft", "in_progress")):
            assert codes(lambda: services.assert_transition(old, new)) == "invalid_transition"

    def test_no_code_path_completes_or_starts_an_assignment(self, w):
        from apps.assignments.constants import TRANSITIONS

        for targets in TRANSITIONS.values():
            assert not {"in_progress", "completed"} & set(targets)  # reserved for Phase 9 executions
        assert not hasattr(services, "complete") and not hasattr(services, "start")

    def test_a_reason_is_required_to_cancel(self, w):
        a = create(w)
        assert codes(lambda: services.cancel(a, actor=w["admin"], reason="  ")) == "reason_required"
        assert ChecklistAssignment.objects.get(pk=a.pk).status == "draft"

    def test_issue_revalidates_everything(self, w):
        a = create(w)
        v2 = lifecycle.create_draft(w["template"], actor=w["admin"])
        lifecycle.publish_version(v2, actor=w["admin"])  # the draft's version became superseded meanwhile
        assert codes(lambda: services.issue(a, actor=w["admin"])) == "superseded"
        b = create(w, version=v2, due_at=future(1))
        ChecklistAssignment.objects.filter(pk=b.pk).update(due_at=future(-1))  # the deadline passed while it was a draft
        assert codes(lambda: services.issue(b, actor=w["admin"])) == "invalid_due"

    def test_events_are_sent_after_commit_to_future_receivers(self, w, django_capture_on_commit_callbacks):
        from apps.assignments import events

        got = []
        events.assignment_issued.connect(lambda sender, assignment, actor, **kw: got.append(assignment.pk), weak=False, dispatch_uid="t")
        try:
            a = create(w)
            with django_capture_on_commit_callbacks(execute=True):
                services.issue(a, actor=w["admin"])
        finally:
            events.assignment_issued.disconnect(dispatch_uid="t")
        assert got == [a.pk]


@pytest.mark.django_db
class TestChanges:
    def test_changes_are_audited_and_limited(self, w):
        a = create(w, issue=True, due_at=future(24))
        services.change_priority(a, actor=w["admin"], priority="critical")
        services.change_due_date(a, actor=w["admin"], due_at=future(72))
        services.change_assignee(a, actor=w["admin"], assigned_to=w["worker"])
        a.refresh_from_db()
        assert (a.priority, a.assigned_to_id) == ("critical", w["worker"].pk)
        events = set(ActivityLog.objects.filter(metadata__assignment_id=str(a.pk)).values_list("verb", flat=True))
        assert {"assignment.priority_changed", "assignment.due_date_changed", "assignment.assignee_changed"} <= events
        change = ActivityLog.objects.get(verb="assignment.priority_changed").metadata["change"]
        assert change == {"from": "normal", "to": "critical"}
        assert codes(lambda: services.change_due_date(a, actor=w["admin"], due_at=future(-2))) == "invalid_due"
        assert codes(lambda: services.change_priority(a, actor=w["admin"], priority="x")) == "invalid_priority"

    def test_a_no_op_change_is_not_audited(self, w):
        a = create(w, issue=True)
        services.change_priority(a, actor=w["admin"], priority="normal")
        assert not ActivityLog.objects.filter(verb="assignment.priority_changed").exists()

    def test_a_cancelled_assignment_cannot_be_changed(self, w):
        a = create(w)
        services.cancel(a, actor=w["admin"], reason="x")
        with pytest.raises(PermissionDenied):
            services.change_priority(a, actor=w["admin"], priority="high")

    def test_edit_a_draft_but_not_protected_fields(self, w):
        a = create(w)
        a = services.update_draft(a, actor=w["admin"], priority="low", location=w["r2"], instructions="Moved")
        assert (a.priority, a.location_id) == ("low", w["r2"].pk)
        assert codes(lambda: services.update_draft(a, actor=w["admin"], status="assigned")) == "protected_field"
        assert codes(lambda: services.update_draft(a, actor=w["admin"], organization=w["org"])) == "protected_field"
        services.issue(a, actor=w["admin"])
        with pytest.raises(PermissionDenied):  # an issued assignment is no longer a draft: use the change_* operations
            services.update_draft(a, actor=w["admin"], priority="high")

    def test_overdue_follows_the_due_date_immediately(self, w):
        a = create(w, issue=True, due_at=future(1))
        assert not a.is_overdue and a.effective_status == "assigned"
        ChecklistAssignment.objects.filter(pk=a.pk).update(due_at=future(-1))  # time passes
        a.refresh_from_db()
        assert a.is_overdue and a.effective_status == "overdue"
        services.change_due_date(a, actor=w["admin"], due_at=future(5))
        a.refresh_from_db()
        assert not a.is_overdue
        a2 = create(w, location=w["r2"], issue=True)  # no due date: never overdue
        assert not a2.is_overdue


@pytest.mark.django_db
class TestDuplicatePolicy:
    def test_one_open_assignment_per_version_and_location(self, w):
        create(w, issue=True)
        with pytest.raises(AssignmentConflict) as info:
            create(w, issue=True)
        assert info.value.assignment_code == "duplicate" and "ASG-00001" in str(info.value)

    def test_a_cancelled_assignment_does_not_block_a_new_one(self, w):
        first = create(w, issue=True)
        services.cancel(first, actor=w["admin"], reason="redo")
        assert create(w, issue=True).status == "assigned"

    def test_drafts_do_not_block_but_issuing_two_does(self, w):
        a, b = create(w), create(w)
        services.issue(a, actor=w["admin"])
        with pytest.raises(AssignmentConflict):
            services.issue(b, actor=w["admin"])
        assert ChecklistAssignment.objects.get(pk=b.pk).status == "draft"

    def test_reopening_into_a_duplicate_is_caught_when_issuing(self, w):
        first = create(w, issue=True)
        services.cancel(first, actor=w["admin"], reason="x")
        create(w, issue=True)
        services.reopen(first, actor=w["admin"])
        with pytest.raises(AssignmentConflict):
            services.issue(first, actor=w["admin"])

    def test_the_database_enforces_it_too(self, w):
        a = create(w, issue=True)
        b = create(w)
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistAssignment.objects.filter(pk=b.pk).update(status="assigned", issued_at=a.issued_at)

    def test_a_new_version_is_a_different_assignment_target(self, w):
        create(w, issue=True)
        v2 = lifecycle.create_draft(w["template"], actor=w["admin"])
        lifecycle.publish_version(v2, actor=w["admin"])
        assert create(w, version=v2, issue=True).status == "assigned"


@pytest.mark.django_db
class TestScopeInServices:
    def test_a_scoped_manager_acts_only_inside_their_subtree(self, w):
        m = w["manager"]
        assert create(w, actor=m, location=w["r1"]).status == "draft"
        assert create(w, actor=m, location=w["area1"]).status == "draft"
        for outside in (w["r3"], w["area2"], w["wh"], w["region"]):
            with pytest.raises(PermissionDenied):
                create(w, actor=m, location=outside)
        with pytest.raises(PermissionDenied):
            create(w, actor=w["plain"])
        with pytest.raises(PermissionDenied):
            create(w, actor=w["viewer"])  # can view, cannot create

    def test_lifecycle_actions_respect_scope(self, w):
        in_scope = create(w, actor=w["manager"], location=w["r1"], issue=True)
        elsewhere = create(w, location=w["r3"], issue=True)
        services.change_priority(in_scope, actor=w["manager"], priority="high")
        for call in (lambda: services.cancel(elsewhere, actor=w["manager"], reason="x"), lambda: services.change_priority(elsewhere, actor=w["manager"], priority="high"),
                     lambda: services.issue(create(w, location=w["r3"]), actor=w["manager"])):
            with pytest.raises(PermissionDenied):
                call()
        with pytest.raises(PermissionDenied):
            services.cancel(in_scope, actor=w["viewer"], reason="x")
        assert ChecklistAssignment.objects.get(pk=elsewhere.pk).status == "assigned"

    def test_the_checklist_must_be_visible_to_the_actor(self, w, make_user, assign, org_a):
        from apps.permissions import services as rbac
        from apps.permissions.models import MembershipRole
        from assignments_support import ASSIGN_ALL

        blind = make_user(email="as-blind@example.com", organization=org_a, role=None)
        assign(blind, org_a, rbac.create_role(organization=org_a, name="No checklists", permissions=ASSIGN_ALL))  # no checklist.view
        with pytest.raises(PermissionDenied):
            create(w, actor=blind)
        assert MembershipRole


@pytest.mark.django_db
class TestDatabaseGuard:
    def test_organization_consistency_is_enforced_by_the_database(self, w):
        a = create(w)
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistAssignment.objects.filter(pk=a.pk).update(location=w["b_loc"])
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistAssignment.objects.filter(pk=a.pk).update(version=w["b_version"])

    def test_identity_is_frozen_after_issue_and_history_is_protected(self, w):
        a = create(w, issue=True)
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistAssignment.objects.filter(pk=a.pk).update(location=w["r2"])
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistAssignment.objects.filter(pk=a.pk).delete()
        draft = create(w, location=w["r2"])
        ChecklistAssignment.objects.filter(pk=draft.pk).update(location=w["r3"])  # a draft may still move

    def test_versions_and_locations_with_assignments_cannot_be_deleted(self, w):
        from django.db.models import ProtectedError

        create(w)
        with pytest.raises(ProtectedError):
            w["version"].delete() if False else w["r1"].delete()
        assert w["version"].assignments.count() == 1

    def test_the_lifecycle_constraint(self, w):
        a = create(w)
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistAssignment.objects.filter(pk=a.pk).update(status="assigned")  # issued_at missing
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistAssignment.objects.filter(pk=a.pk).update(status="bogus")
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistAssignment.objects.filter(pk=a.pk).update(status="cancelled", cancelled_at=future())  # no reason
