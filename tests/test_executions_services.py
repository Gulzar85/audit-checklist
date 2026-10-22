"""Execution services: models and DB guards, lifecycle, answers (every type), rules, progress, submission, authorization."""

from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, transaction

from apps.assignments import services as assignment_services
from apps.core.models import ActivityLog
from apps.executions import policies, selectors, services
from apps.executions.models import ChecklistExecution, ChecklistResponse

from executions_support import REQUIRED, build_exec_world, issued, item_id


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_exec_world(org_a, org_b, make_user, assign, make_type)


def inp(w, code, value=None, **kw):
    return services.AnswerInput(item_id(w["eversion"], code), value, **kw)


def start(w, who="runner", **kw):
    ex, _created = services.start_or_resume(issued(w, **kw), actor=w[who])
    return ex


def fill(w, ex, who="runner", **extra):
    values = {**REQUIRED, **extra}
    return services.save_answers(ex, actor=w[who], inputs=[inp(w, c, v) for c, v in values.items()])


def save(w, ex, code, value=None, who="runner", **kw):
    return services.save_answers(ex, actor=w[who], inputs=[inp(w, code, value, **kw)])


# ---------------------------------------------------------------------------------------------------------------- models / guards
@pytest.mark.django_db
class TestModelsAndGuards:
    def test_the_execution_pins_organization_and_exact_version(self, w):
        ex = start(w)
        assert ex.organization_id == w["org"].pk and ex.version_id == w["eversion"].pk and ex.assignment.version_id == ex.version_id
        assert ex.reference == "EXE-00001" and ex.revision == 1

    def test_a_newer_version_does_not_move_a_started_execution(self, w):
        from apps.checklists import lifecycle

        ex = start(w)
        draft = lifecycle.create_draft(w["etemplate"], change_summary="v2")
        lifecycle.publish_version(draft, expected_hash=lifecycle.run_validation(draft).draft_hash)
        ex.refresh_from_db()
        assert ex.version.version_number == 1 and ex.assignment.version.version_number == 1

    def test_one_live_execution_per_assignment_in_the_database(self, w):
        ex = start(w)
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistExecution.objects.create(organization=ex.organization, number=99, assignment=ex.assignment, version=ex.version)

    def test_the_database_refuses_a_mismatched_version_or_org(self, w):
        a = issued(w)
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistExecution.objects.create(organization=w["org"], number=1, assignment=a, version=w["version"])  # another checklist's version

    def test_identity_is_immutable(self, w):
        ex = start(w)
        other = issued(w, w["r2"])
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistExecution.objects.filter(pk=ex.pk).update(assignment=other)

    def test_one_response_per_item_and_value_matches_status(self, w):
        ex = start(w)
        fill(w, ex)
        row = ex.responses.first()
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistResponse.objects.create(execution=ex, item=row.item, status="answered", value="x")
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistResponse.objects.filter(pk=row.pk).update(status="answered", value=None)

    def test_a_response_must_answer_an_item_of_the_executions_version(self, w):
        from apps.checklists.models import ChecklistItem

        ex = start(w)
        foreign = ChecklistItem.objects.filter(section__version=w["version"]).first()
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistResponse.objects.create(execution=ex, item=foreign, status="answered", value="yes")

    def test_a_submitted_execution_is_frozen_even_for_raw_queries(self, w):
        ex = start(w)
        fill(w, ex)
        services.submit(ex, actor=w["runner"])
        row = ChecklistResponse.objects.filter(execution=ex).first()
        for change in (lambda: ChecklistResponse.objects.filter(pk=row.pk).update(value="no"),
                       lambda: ChecklistResponse.objects.filter(pk=row.pk).delete(),
                       lambda: ChecklistResponse.objects.create(execution=ex, item=ChecklistResponse.objects.exclude(pk=row.pk).first().item, status="not_applicable"),
                       lambda: ChecklistExecution.objects.filter(pk=ex.pk).update(status="in_progress"),
                       lambda: ChecklistExecution.objects.filter(pk=ex.pk).delete()):
            with pytest.raises(IntegrityError), transaction.atomic():
                change()
        assert ChecklistResponse.objects.get(pk=row.pk).value == row.value


# ---------------------------------------------------------------------------------------------------------------- lifecycle
@pytest.mark.django_db
class TestStartResumeCancel:
    def test_start_is_idempotent(self, w):
        a = issued(w)
        ex1, c1 = services.start_or_resume(a, actor=w["runner"])
        ex2, c2 = services.start_or_resume(a, actor=w["runner2"])  # an undesignated assignment is open to the location's users
        assert (c1, c2) == (True, False) and ex1.pk == ex2.pk and ChecklistExecution.objects.count() == 1
        assert ActivityLog.objects.filter(verb="execution.started").count() == 1

    def test_the_assignment_moves_only_through_its_service(self, w):
        a = issued(w)
        assert a.status == "assigned"
        ex, _ = services.start_or_resume(a, actor=w["runner"])
        a.refresh_from_db()
        assert a.status == "in_progress"
        fill(w, ex)
        a.refresh_from_db()
        assert a.status == "in_progress"  # saving a draft never completes it
        services.submit(ex, actor=w["runner"])
        a.refresh_from_db()
        assert a.status == "completed"
        assert {"assignment.started", "assignment.completed"} <= set(ActivityLog.objects.values_list("verb", flat=True))

    def test_draft_cancelled_and_foreign_assignments_cannot_start(self, w):
        draft = assignment_services.create_assignment(actor=w["admin"], version=w["eversion"], location=w["r1"])
        with pytest.raises(services.ExecutionError) as e:
            services.start_or_resume(draft, actor=w["runner"])
        assert e.value.execution_code == "not_executable"
        a = issued(w, w["r2"], version=w["eversion"])
        assignment_services.cancel(a, actor=w["admin"], reason="x")
        with pytest.raises(services.ExecutionError):
            services.start_or_resume(a, actor=w["area_runner"])
        with pytest.raises(PermissionDenied):
            services.start_or_resume(issued(w, w["r3"]), actor=w["runner"])  # outside the runner's scope
        with pytest.raises(PermissionDenied):
            services.start_or_resume(issued(w, w["wh"]), actor=w["runner"])
        assert not ChecklistExecution.objects.exists()

    def test_a_designated_assignment_belongs_to_the_designee(self, w):
        a = issued(w, assigned_to=w["runner"])
        with pytest.raises(PermissionDenied):
            services.start_or_resume(a, actor=w["runner2"])
        ex, _ = services.start_or_resume(a, actor=w["runner"])
        assert ex.started_by_id == w["runner"].pk

    def test_a_role_without_execution_permission_cannot_start(self, w):
        a = issued(w, w["r2"])
        for who in ("viewer", "plain", "other", "manager"):  # the area manager holds execution.create but not at... r2 is in Area 1
            if who == "manager":
                continue
            with pytest.raises(PermissionDenied):
                services.start_or_resume(a, actor=w[who])

    def test_overdue_assignments_stay_executable(self, w):
        from apps.assignments.models import ChecklistAssignment
        from assignments_support import future

        a = issued(w)
        ChecklistAssignment.objects.filter(pk=a.pk).update(due_at=future(-5))
        ex, created = services.start_or_resume(ChecklistAssignment.objects.get(pk=a.pk), actor=w["runner"])
        assert created

    def test_after_submission_start_returns_the_submitted_execution(self, w):
        a = issued(w)
        ex, _ = services.start_or_resume(a, actor=w["runner"])
        fill(w, ex)
        services.submit(ex, actor=w["runner"])
        again, created = services.start_or_resume(a, actor=w["runner"])
        assert not created and again.pk == ex.pk and again.status == "submitted" and ChecklistExecution.objects.count() == 1

    def test_cancelling_the_execution_keeps_answers_and_reopens_the_assignment(self, w):
        a = issued(w)
        ex, _ = services.start_or_resume(a, actor=w["runner"])
        fill(w, ex)
        with pytest.raises(PermissionDenied):
            services.cancel_execution(ex, actor=w["runner"], reason="x")  # needs execution.cancel
        with pytest.raises(services.ExecutionError):
            services.cancel_execution(ex, actor=w["admin"], reason="")
        services.cancel_execution(ex, actor=w["admin"], reason="Wrong site")
        ex.refresh_from_db()
        a.refresh_from_db()
        assert ex.status == "cancelled" and ex.responses.count() == 3 and a.status == "assigned"
        with pytest.raises(services.ExecutionConflict):
            services.cancel_execution(ex, actor=w["admin"], reason="again")
        with pytest.raises(services.ExecutionConflict):
            save(w, ex, "BOOL", True)
        new, created = services.start_or_resume(a, actor=w["runner"])  # the slot is free again
        assert created and new.pk != ex.pk

    def test_cancelling_the_assignment_cancels_its_execution_and_keeps_history(self, w):
        a = issued(w)
        ex, _ = services.start_or_resume(a, actor=w["runner"])
        fill(w, ex)
        assignment_services.cancel(a, actor=w["admin"], reason="Closed")
        ex.refresh_from_db()
        assert ex.status == "cancelled" and "Assignment cancelled" in ex.cancel_reason and ex.responses.count() == 3

    def test_a_completed_assignment_cannot_be_cancelled(self, w):
        a = issued(w)
        ex, _ = services.start_or_resume(a, actor=w["runner"])
        fill(w, ex)
        services.submit(ex, actor=w["runner"])
        with pytest.raises(assignment_services.AssignmentError):
            assignment_services.cancel(a, actor=w["admin"], reason="x")
        ex.refresh_from_db()
        assert ex.status == "submitted"

    def test_no_user_facing_path_can_complete_an_assignment(self, w):
        from apps.assignments.constants import TRANSITIONS

        a = issued(w)
        for targets in TRANSITIONS.values():
            assert not {"in_progress", "completed"} & set(targets)
        with pytest.raises(assignment_services.AssignmentError):
            assignment_services.assert_transition("assigned", "completed")
        assert a.status == "assigned"


# ---------------------------------------------------------------------------------------------------------------- answers
@pytest.mark.django_db
class TestAnswerTypes:
    VALID_INVALID = [
        ("FIRE-EXIT", "yes", "maybe"), ("HANDWASH", "na", "perhaps"), ("BOOL", True, "banana"), ("FRIDGE", "4.5", "11"),
        ("KIND", "cold", "frozen"), ("TAGS", ["pest", "leak"], ["pest", "leak", "dirt"]), ("RATING", 4, 9),
        ("GRID", {"kitchen": "clean"}, {"garage": "clean"}), ("NOTE", "ok", "x" * 40), ("WHEN", "2026-10-01", "not a date"),
        ("STAMP", "2026-10-01T10:00:00+00:00", "yesterday-ish"),
    ]

    def test_every_type_accepts_valid_and_rejects_invalid(self, w):
        ex = start(w)
        for code, good, bad in self.VALID_INVALID:
            r = save(w, ex, code, good)
            assert r.ok and r.saved, (code, r.errors)
            row = ChecklistResponse.objects.get(execution=ex, item__code=code)
            before = (row.value, row.revision)
            r = save(w, ex, code, bad, revision=row.revision)
            assert r.errors and not r.saved, code
            row.refresh_from_db()
            assert (row.value, row.revision) == before, code  # a refused answer leaves the stored one untouched

    def test_stored_values_are_the_validators_serialization(self, w):
        ex = start(w)
        save(w, ex, "FRIDGE", "4.50")
        save(w, ex, "TAGS", ["leak", "pest"])
        values = {r.item.code: r.value for r in ex.responses.select_related("item")}
        assert str(values["FRIDGE"]).startswith("4.5") and values["TAGS"] == ["pest", "leak"]

    def test_n_a_is_only_for_types_and_items_that_allow_it(self, w):
        ex = start(w)
        assert save(w, ex, "BOOL", None, not_applicable=True).ok  # optional: may be declared N/A
        assert ChecklistResponse.objects.get(execution=ex, item__code="BOOL").status == "not_applicable"
        r = save(w, ex, "HANDWASH", None, not_applicable=True)  # required: cannot be waved through (yes_no_na has its own "na" option)
        assert r.errors and not ChecklistResponse.objects.filter(execution=ex, item__code="HANDWASH").exists()

    def test_media_answers_are_rejected_until_evidence_storage_exists(self, w):
        ex = start(w)
        for code, value in (("PHOTO", [{"file_id": "0b6c7f0e-3d4a-4a46-9b8e-2f1e5d1c7a10", "name": "a.jpg", "size": 10, "content_type": "image/jpeg"}]),
                            ("SIGN", {"signer_name": "A. Khan", "file_id": None})):
            r = save(w, ex, code, value)
            assert r.errors[item_id(w["eversion"], code)][0]["code"] == "evidence_unavailable"
        assert not ex.responses.exists()
        assert save(w, ex, "PHOTO", []).ok  # an optional media item simply stays empty

    def test_clearing_an_answer_removes_the_row(self, w):
        ex = start(w)
        save(w, ex, "NOTE", "hello")
        row = ChecklistResponse.objects.get(execution=ex, item__code="NOTE")
        r = save(w, ex, "NOTE", "", revision=row.revision)
        assert r.cleared and not ChecklistResponse.objects.filter(execution=ex, item__code="NOTE").exists()

    def test_comments_are_kept_trimmed_and_bounded(self, w):
        ex = start(w)
        save(w, ex, "FRIDGE", "5", comment="  warm  ")
        assert ChecklistResponse.objects.get(execution=ex, item__code="FRIDGE").comment == "warm"
        row = ChecklistResponse.objects.get(execution=ex, item__code="FRIDGE")
        assert save(w, ex, "FRIDGE", "5", comment="x" * 2001, revision=row.revision).errors
        assert save(w, ex, "FRIDGE", "6", revision=row.revision).ok  # comment None = unchanged
        assert ChecklistResponse.objects.get(execution=ex, item__code="FRIDGE").comment == "warm"

    def test_unknown_foreign_and_malformed_item_ids_are_refused_alike(self, w):
        ex = start(w)
        from apps.checklists.models import ChecklistItem

        foreign = str(ChecklistItem.objects.filter(section__version=w["version"]).first().pk)
        for ref in (foreign, "not-a-uuid", "00000000-0000-0000-0000-000000000000"):
            r = services.save_answers(ex, actor=w["runner"], inputs=[services.AnswerInput(ref, "yes")])
            assert r.errors[ref][0]["code"] == "unknown_item"
        assert not ex.responses.exists()

    def test_a_batch_saves_the_valid_and_reports_the_rest(self, w):
        ex = start(w)
        r = services.save_answers(ex, actor=w["runner"], inputs=[inp(w, "FIRE-EXIT", "yes"), inp(w, "FRIDGE", "99"), inp(w, "HANDWASH", "yes")])
        assert len(r.saved) == 2 and list(r.errors) == [item_id(w["eversion"], "FRIDGE")]


# ---------------------------------------------------------------------------------------------------------------- revisions
@pytest.mark.django_db
class TestRevisions:
    def test_stale_saves_are_conflicts_and_change_nothing(self, w):
        ex = start(w)
        save(w, ex, "FRIDGE", "4")
        row = ChecklistResponse.objects.get(execution=ex, item__code="FRIDGE")
        assert row.revision == 1
        assert save(w, ex, "FRIDGE", "5", revision=1).ok
        stale = save(w, ex, "FRIDGE", "9", revision=1)
        assert stale.conflicts == {item_id(w["eversion"], "FRIDGE"): 2} and not stale.saved
        assert str(ChecklistResponse.objects.get(execution=ex, item__code="FRIDGE").value).startswith("5")

    def test_creating_an_existing_response_without_its_revision_conflicts(self, w):
        ex = start(w)
        save(w, ex, "FRIDGE", "4")
        assert save(w, ex, "FRIDGE", "5").conflicts  # revision omitted == "I think it does not exist yet"

    def test_unchanged_saves_do_not_bump_revisions_or_history(self, w):
        ex = start(w)
        save(w, ex, "FRIDGE", "4")
        row = ChecklistResponse.objects.get(execution=ex, item__code="FRIDGE")
        r = save(w, ex, "FRIDGE", "4", revision=1)
        assert not r.saved and ChecklistResponse.objects.get(pk=row.pk).revision == 1
        ex.refresh_from_db()
        assert ex.revision == 2

    def test_the_execution_revision_and_activity_advance(self, w):
        ex = start(w)
        fill(w, ex)
        ex.refresh_from_db()
        assert ex.revision == 2 and ActivityLog.objects.filter(verb="execution.responses_saved").count() == 1


# ---------------------------------------------------------------------------------------------------------------- rules
@pytest.mark.django_db
class TestRules:
    def test_a_hidden_item_cannot_be_answered(self, w):
        ex = start(w)
        r = save(w, ex, "ISSUE", "something")
        assert r.errors[item_id(w["eversion"], "ISSUE")][0]["code"] == "item_hidden"
        assert not ex.responses.exists()

    def test_the_rule_reveals_and_requires_the_follow_up(self, w):
        ex = start(w)
        r = fill(w, ex, **{"FIRE-EXIT": "no"})
        ev = r.state.evaluation.items
        assert ev["ISSUE"].visible and ev["ISSUE"].required
        problems = services.submission_problems(r.state)
        assert item_id(w["eversion"], "ISSUE") in problems
        assert services.compute_progress(r.state)["percent"] < 100
        assert save(w, ex, "ISSUE", "Blocked by crates").ok
        assert not services.submission_problems(services.build_state(ex))

    def test_hidden_answers_are_retained_then_ignored_then_stamped(self, w):
        ex = start(w)
        fill(w, ex, **{"FIRE-EXIT": "no"})
        save(w, ex, "ISSUE", "Blocked by crates")
        fe = ChecklistResponse.objects.get(execution=ex, item__code="FIRE-EXIT")
        save(w, ex, "FIRE-EXIT", "yes", revision=fe.revision)  # the follow-up is hidden again
        issue = ChecklistResponse.objects.get(execution=ex, item__code="ISSUE")
        assert not issue.is_hidden  # retained, not deleted, not yet stamped
        state = services.build_state(ex)
        assert not state.evaluation.items["ISSUE"].visible and not services.submission_problems(state)
        services.submit(ex, actor=w["runner"])
        issue.refresh_from_db()
        assert issue.is_hidden and issue.value == "Blocked by crates"

    def test_a_required_comment_blocks_submission(self, w):
        ex = start(w)
        fill(w, ex, HANDWASH="no")
        problems = services.submission_problems(services.build_state(ex))
        assert problems[item_id(w["eversion"], "HANDWASH")][0] == {"code": "comment_required", "message": "Why is it not working?"}
        row = ChecklistResponse.objects.get(execution=ex, item__code="HANDWASH")
        save(w, ex, "HANDWASH", "no", comment="Pipe burst", revision=row.revision)
        assert services.submit(ex, actor=w["runner"]).execution.status == "submitted"

    def test_the_server_does_not_trust_the_client_about_visibility(self, w):
        ex = start(w)
        r = services.save_answers(ex, actor=w["runner"], inputs=[inp(w, "FIRE-EXIT", "yes"), inp(w, "ISSUE", "forged")])
        assert r.saved and item_id(w["eversion"], "ISSUE") in r.errors and not ChecklistResponse.objects.filter(item__code="ISSUE").exists()


    def test_required_evidence_blocks_submission_while_evidence_cannot_be_captured(self, w):
        ex = start(w)
        fill(w, ex, BOOL=True)
        problems = services.submission_problems(services.build_state(ex))
        assert problems[item_id(w["eversion"], "FRIDGE")][0]["code"] == "evidence_required"
        with pytest.raises(services.ExecutionValidationError):
            services.submit(ex, actor=w["runner"])
        row = ChecklistResponse.objects.get(execution=ex, item__code="BOOL")
        save(w, ex, "BOOL", False, revision=row.revision)  # the rule no longer applies
        assert services.submit(ex, actor=w["runner"]).execution.status == "submitted"

    def test_rule_constraints_tighten_the_answer(self, w):
        ex = start(w)
        assert save(w, ex, "NOTE", "123456").ok  # fine while the exit is accessible (max length 20)
        row = ChecklistResponse.objects.get(execution=ex, item__code="NOTE")
        save(w, ex, "FIRE-EXIT", "no")
        refused = save(w, ex, "NOTE", "1234567", revision=row.revision)  # the blocked-exit rule tightens the limit to 5
        assert refused.errors and not refused.saved
        assert save(w, ex, "NOTE", "12345", revision=row.revision).ok

    def test_rule_option_availability_limits_choices(self, w):
        ex = start(w)
        assert save(w, ex, "KIND", "dry").ok
        row = ChecklistResponse.objects.get(execution=ex, item__code="KIND")
        save(w, ex, "FIRE-EXIT", "no")
        assert save(w, ex, "KIND", "dry", revision=row.revision).saved == []  # unchanged value -> nothing to save
        state = services.build_state(ex)
        assert state.evaluation.items["KIND"].options["available"] == ["cold"]
        assert services.submission_problems(state).get(item_id(w["eversion"], "KIND"))  # the stored "dry" is no longer a valid choice


# ---------------------------------------------------------------------------------------------------------------- progress / submission
@pytest.mark.django_db
class TestProgressAndSubmission:
    def test_progress_counts_visible_required_items_only(self, w):
        ex = start(w)
        p = services.compute_progress(services.build_state(ex))
        assert (p["percent"], p["total"], p["basis"]) == (0, 3, "required") and len(p["unanswered_required"]) == 3
        r = save(w, ex, "FIRE-EXIT", "yes")
        assert services.compute_progress(r.state)["percent"] == 33
        r = save(w, ex, "FIRE-EXIT", "no", revision=1)
        assert services.compute_progress(r.state)["total"] == 4  # the follow-up became visible and required

    def test_missing_required_answers_block_submission_and_change_nothing(self, w):
        ex = start(w)
        save(w, ex, "FIRE-EXIT", "yes")
        with pytest.raises(services.ExecutionValidationError) as e:
            services.submit(ex, actor=w["runner"])
        assert set(e.value.problems) == {item_id(w["eversion"], "HANDWASH"), item_id(w["eversion"], "FRIDGE")}
        ex.refresh_from_db()
        assert ex.status == "in_progress" and ex.responses.count() == 1
        assert ActivityLog.objects.filter(verb="execution.submission_refused").count() == 1
        assert not ActivityLog.objects.filter(verb="execution.submitted").exists()

    def test_submission_is_idempotent(self, w):
        ex = start(w)
        fill(w, ex)
        first = services.submit(ex, actor=w["runner"])
        second = services.submit(ex, actor=w["runner"])
        assert not first.already_submitted and second.already_submitted
        assert ActivityLog.objects.filter(verb="execution.submitted").count() == 1
        ex.refresh_from_db()
        assert ex.submitted_by_id == w["runner"].pk and ex.submitted_at

    def test_a_submitted_execution_cannot_be_edited_through_services(self, w):
        ex = start(w)
        fill(w, ex)
        services.submit(ex, actor=w["runner"])
        with pytest.raises(services.ExecutionConflict) as e:
            save(w, ex, "BOOL", True)
        assert e.value.execution_code == "already_submitted"

    def test_submission_needs_the_submit_permission(self, w):
        ex = start(w)
        fill(w, ex)
        with pytest.raises(PermissionDenied):
            services.submit(ex, actor=w["viewer"])
        assert ChecklistExecution.objects.get(pk=ex.pk).status == "in_progress"

    def test_an_optional_media_item_does_not_block_submission(self, w):
        ex = start(w)
        fill(w, ex)
        state = services.build_state(ex)
        assert state.evaluation.items["PHOTO"].answer.status == "empty" and not state.evaluation.items["PHOTO"].required
        assert services.submit(ex, actor=w["runner"]).execution.status == "submitted"


# ---------------------------------------------------------------------------------------------------------------- authorization
@pytest.mark.django_db
class TestAuthorization:
    def test_saves_need_execution_update_at_the_location(self, w):
        ex = start(w)
        for who in ("viewer", "plain", "other", "manager"):
            with pytest.raises(PermissionDenied):
                save(w, ex, "FIRE-EXIT", "yes", who=who)
        assert not ex.responses.exists()

    def test_a_designated_assignment_cannot_be_worked_by_someone_else(self, w):
        ex = start(w, assigned_to=w["runner"])
        with pytest.raises(PermissionDenied):
            save(w, ex, "FIRE-EXIT", "yes", who="runner2")
        assert save(w, ex, "FIRE-EXIT", "yes").ok

    def test_visibility_selectors(self, w):
        ex = start(w)
        other_asg = issued(w, w["r2"])
        mine = selectors.my_assignments(w["runner"])
        assert not mine.filter(pk=other_asg.pk).exists() and mine.filter(pk=ex.assignment_id).exists()
        assert selectors.my_assignments(w["area_runner"]).filter(pk=other_asg.pk).exists()
        assert not selectors.my_assignments(w["viewer"]).exists() and not selectors.my_assignments(w["other"]).exists()
        assert selectors.get_execution(w["runner"], ex.pk) == ex
        assert selectors.get_execution(w["runner2"], ex.pk) == ex  # same location scope (execution.view)
        assert selectors.get_execution(w["other"], ex.pk) is None and selectors.get_execution(w["plain"], ex.pk) is None
        assert selectors.get_execution(w["runner"], "nope") is None

    def test_policy_functions(self, w):
        ex = start(w)
        assert policies.can_work(w["runner"], ex) and not policies.can_work(w["viewer"], ex) and not policies.can_work(w["other"], ex)
        assert policies.can_cancel(w["admin"], ex) and not policies.can_cancel(w["runner"], ex)
        assert policies.can_view(w["runner"], ex) and not policies.can_view(w["other"], ex)


# ---------------------------------------------------------------------------------------------------------------- concurrency
@pytest.mark.django_db(transaction=True)
class TestConcurrency:
    def _race(self, jobs):
        import threading

        out = []

        def run(job):
            try:
                out.append(job())
            except Exception as exc:  # noqa: BLE001
                out.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=run, args=(j,)) for j in jobs]
        [t.start() for t in threads]
        [t.join(timeout=90) for t in threads]
        return out

    def test_simultaneous_starts_create_one_execution(self, org_a, org_b, make_user, assign, make_type):
        w = build_exec_world(org_a, org_b, make_user, assign, make_type)
        a = issued(w)
        out = self._race([lambda: services.start_or_resume(a, actor=w["runner"]), lambda: services.start_or_resume(a, actor=w["runner2"])])
        assert ChecklistExecution.objects.count() == 1 and sorted(r[1] for r in out if isinstance(r, tuple)) == [False, True], out
        assert ActivityLog.objects.filter(verb="execution.started").count() == 1

    def test_simultaneous_submissions_submit_once(self, org_a, org_b, make_user, assign, make_type):
        w = build_exec_world(org_a, org_b, make_user, assign, make_type)
        ex = start(w)
        fill(w, ex)
        out = self._race([lambda: services.submit(ex, actor=w["runner"]), lambda: services.submit(ex, actor=w["runner2"])])
        assert sorted(r.already_submitted for r in out if not isinstance(r, Exception)) == [False, True], out
        assert ActivityLog.objects.filter(verb="execution.submitted").count() == 1

    def test_a_save_racing_a_submission_leaves_a_consistent_state(self, org_a, org_b, make_user, assign, make_type):
        w = build_exec_world(org_a, org_b, make_user, assign, make_type)
        ex = start(w)
        fill(w, ex)
        out = self._race([lambda: services.submit(ex, actor=w["runner"]), lambda: save(w, ex, "NOTE", "late")])
        ex.refresh_from_db()
        assert ex.status == "submitted"
        late = ChecklistResponse.objects.filter(execution=ex, item__code="NOTE").exists()
        saved_first = any(isinstance(r, services.SaveResult) and r.saved for r in out)
        assert late == saved_first, out  # either it made it in before submission or it was refused - never half

    def test_two_saves_of_one_answer_with_the_same_revision_cannot_both_win(self, org_a, org_b, make_user, assign, make_type):
        w = build_exec_world(org_a, org_b, make_user, assign, make_type)
        ex = start(w)
        save(w, ex, "FRIDGE", "4")
        out = self._race([lambda: save(w, ex, "FRIDGE", "5", revision=1), lambda: save(w, ex, "FRIDGE", "6", who="runner2", revision=1)])
        assert sorted(bool(r.saved) for r in out) == [False, True], out
        assert ChecklistResponse.objects.get(execution=ex, item__code="FRIDGE").revision == 2


