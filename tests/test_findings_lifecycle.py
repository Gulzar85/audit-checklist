"""Phase 12 services: manual creation, controlled edits, assignment, the transition matrix, closure policy, configuration, concurrency."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.utils import timezone

from apps.core.models import ActivityLog
from apps.findings import hooks, services
from apps.findings.constants import TRANSITIONS
from apps.findings.models import Finding, FindingCategory, FindingSeverity

from findings_support import build_findings_world, submit


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_findings_world(org_a, org_b, make_user, assign, make_type)


def cat(w, code="safety", org=None):
    return FindingCategory.objects.get(organization=org or w["org_a"], code=code)


def sev(w, code="medium", org=None):
    return FindingSeverity.objects.get(organization=org or w["org_a"], code=code)


def manual(w, who="raiser", location=None, **kw):
    kw.setdefault("title", "Cracked floor tile")
    kw.setdefault("category", cat(w))
    kw.setdefault("severity", sev(w))
    return services.create_manual(actor=w[who], location=location or w["r1"], **kw)


def future(days=3):
    return timezone.now() + timedelta(days=days)


class TestManualCreation:
    def test_an_authorized_user_creates_a_finding_and_is_recorded_as_the_creator(self, w):
        f = manual(w, description="Near the entrance.")
        assert (f.status, f.source_type, f.detected_by, f.reference) == ("open", "manual", w["raiser"], "FND-00001")
        assert f.execution_id is None and f.detection_key == "" and f.organization_id == w["org_a"].pk
        assert ActivityLog.objects.filter(verb="finding.created", metadata__finding_id=str(f.pk), actor=w["raiser"]).exists()

    def test_creating_requires_the_permission_at_the_location(self, w):
        with pytest.raises(PermissionDenied):
            manual(w, who="viewer")
        with pytest.raises(PermissionDenied):
            manual(w, who="raiser", location=w["r2"])  # the raiser is scoped to Restaurant 1

    def test_the_category_and_severity_must_be_active_and_in_the_organization(self, w, org_b):
        services.ensure_defaults(org_b)
        for kw in ({"category": cat(w, org=org_b)}, {"severity": sev(w, org=org_b)}):
            with pytest.raises(services.FindingError) as exc:
                manual(w, **kw)
            assert exc.value.finding_code in ("invalid_category", "invalid_severity")
        services.save_category(actor=w["admin"], organization=w["org_a"], category=cat(w, "other"), name="Other", is_active=False)
        with pytest.raises(services.FindingError):
            manual(w, category=cat(w, "other"))

    def test_text_rules_and_the_future_due_date(self, w):
        for kw, code in (({"title": "  "}, "required_field"), ({"title": "x" * 201}, "too_long"), ({"due_at": timezone.now() - timedelta(hours=1)}, "invalid_due"),
                         ({"description": "d" * 2001}, "too_long")):
            with pytest.raises(services.FindingError) as exc:
                manual(w, **kw)
            assert exc.value.finding_code == code
        assert manual(w, due_at=future()).due_at

    def test_severity_policy_owner_required_and_default_due_days(self, w):
        with pytest.raises(services.FindingError) as exc:
            manual(w, severity=sev(w, "high"))
        assert exc.value.finding_code == "owner_required"
        assert manual(w, severity=sev(w, "high"), assigned_to=w["triager"]).assigned_to == w["triager"]
        services.save_severity(actor=w["admin"], organization=w["org_a"], severity=sev(w, "low"), name="Low", rank=10, default_due_days=7)
        f = manual(w, severity=sev(w, "low"))
        assert f.due_at and abs((f.due_at - f.detected_at) - timedelta(days=7)) < timedelta(seconds=5)
        assert manual(w).due_at is None  # nothing is invented when the policy sets no due date

    def test_the_assignee_must_be_eligible(self, w):
        with pytest.raises(services.FindingError) as exc:
            manual(w, assigned_to=w["viewer"])  # can view, cannot work findings
        assert exc.value.finding_code == "invalid_assignee"
        with pytest.raises(services.FindingError):
            manual(w, assigned_to=w["r2_viewer"])

    def test_linking_an_execution_validates_every_relationship(self, w, org_b):
        ex = submit(w, {"EXIT": "yes"})
        item = ex.version.sections.first().items.first()
        with pytest.raises(services.FindingError) as unseen:  # finding.create does not reveal an execution the user cannot see
            manual(w, execution=ex, item=item)
        assert unseen.value.finding_code == "unknown_source"
        f = manual(w, who="admin", execution=ex, item=item)
        assert (f.execution_id, f.assignment_id, f.version_id, f.item_id) == (ex.pk, ex.assignment_id, ex.version_id, item.pk)
        assert f.response is not None and f.response.item_id == item.pk
        with pytest.raises(services.FindingError):  # the execution happened at Restaurant 1, not 2
            services.create_manual(actor=w["admin"], location=w["r2"], title="x", category=cat(w), severity=sev(w), execution=ex)
        from apps.checklists.models import ChecklistItem

        stranger = ChecklistItem.objects.filter(section__version=w["eversion"]).first()  # an item of a *different* version
        with pytest.raises(services.FindingError) as exc:
            manual(w, who="admin", execution=ex, item=stranger)
        assert exc.value.finding_code == "invalid_source"
        with pytest.raises(services.FindingError):
            manual(w, who="admin", item=item)  # an item without its execution

    def test_a_cancelled_execution_cannot_be_a_source(self, w):
        from apps.executions import services as exec_services
        from apps.executions.services import lifecycle as exec_lifecycle
        from executions_support import issued

        ex, _ = exec_services.start_or_resume(issued(w, version=w["fversion"]), actor=w["runner"])
        exec_lifecycle.cancel_execution(ex, actor=w["admin"], reason="Wrong site")
        with pytest.raises(services.FindingError) as exc:
            manual(w, who="admin", execution=ex)
        assert exc.value.finding_code in ("invalid_source", "unknown_source")


class TestEditAndAssign:
    def test_triage_fields_change_with_before_after_in_the_audit(self, w):
        f = manual(w)
        services.update_finding(f, actor=w["editor"], title="Cracked tile", severity=sev(w, "low"), category=cat(w, "equipment"), due_at=future())
        f.refresh_from_db()
        assert (f.title, f.severity.code, f.category.code) == ("Cracked tile", "low", "equipment") and f.due_at
        entry = ActivityLog.objects.get(verb="finding.updated", metadata__finding_id=str(f.pk))
        assert entry.metadata["changes"]["title"] == {"from": "Cracked floor tile", "to": "Cracked tile"}
        for verb in ("finding.severity_changed", "finding.category_changed", "finding.due_date_changed"):
            assert ActivityLog.objects.filter(verb=verb, metadata__finding_id=str(f.pk)).exists()

    def test_an_unchanged_edit_is_a_no_op(self, w):
        f = manual(w)
        before = ActivityLog.objects.count()
        services.update_finding(f, actor=w["editor"], title=f.title)
        assert ActivityLog.objects.count() == before

    def test_editing_needs_the_permission_and_an_editable_state(self, w):
        f = manual(w)
        with pytest.raises(PermissionDenied):
            services.update_finding(f, actor=w["raiser"], title="x")
        services.transition(f, "cancel", actor=w["editor"], reason="Error")
        with pytest.raises(services.FindingConflict):
            services.update_finding(f, actor=w["editor"], title="x")

    def test_assignment_validates_eligibility_and_audits(self, w):
        f = manual(w)
        services.assign(f, actor=w["editor"], assigned_to=w["triager"], due_at=future())
        f.refresh_from_db()
        assert f.assigned_to == w["triager"] and f.due_at
        assert ActivityLog.objects.filter(verb="finding.assigned", metadata__finding_id=str(f.pk)).exists()
        with pytest.raises(services.FindingError):
            services.assign(f, actor=w["editor"], assigned_to=w["viewer"])
        with pytest.raises(PermissionDenied):
            services.assign(f, actor=w["triager"], assigned_to=None)
        services.assign(f, actor=w["editor"], assigned_to=None)
        f.refresh_from_db()
        assert f.assigned_to is None


class TestTransitionMatrix:
    def test_the_documented_happy_path(self, w):
        f = manual(w)
        services.transition(f, "acknowledge", actor=w["triager"])
        services.transition(f, "start", actor=w["triager"])
        f = services.transition(f, "resolve", actor=w["triager"], reason="Tile replaced.")
        assert (f.status, f.resolution_summary, f.resolved_by) == ("resolved", "Tile replaced.", w["triager"]) and f.closed_at is None
        f = services.transition(f, "close", actor=w["editor"])
        assert (f.status, f.closed_by) == ("closed", w["editor"]) and f.closed_at
        assert [e.verb for e in ActivityLog.objects.filter(metadata__finding_id=str(f.pk)).order_by("created_at")][1:] == [
            "finding.acknowledged", "finding.started", "finding.resolved", "finding.closed"]

    def test_every_invalid_move_is_refused_and_changes_nothing(self, w):
        for action, spec in TRANSITIONS.items():
            for status in ("open", "acknowledged", "in_progress", "resolved", "closed", "reopened", "cancelled"):
                if status in spec.sources or status == spec.target:
                    continue
                f = manual(w, title=f"{action} from {status}")
                Finding.objects.filter(pk=f.pk).update(**_state(status))
                with pytest.raises(services.FindingError) as exc:
                    services.transition(f, action, actor=w["editor"], reason="because")
                assert exc.value.finding_code == "invalid_transition"
                assert Finding.objects.get(pk=f.pk).status == status

    def test_a_repeat_is_a_conflict_not_a_second_change(self, w):
        f = manual(w)
        services.transition(f, "acknowledge", actor=w["triager"])
        with pytest.raises(services.FindingConflict) as exc:
            services.transition(f, "acknowledge", actor=w["triager"])
        assert exc.value.finding_code == "already_acknowledged"
        assert ActivityLog.objects.filter(verb="finding.acknowledged", metadata__finding_id=str(f.pk)).count() == 1

    def test_permissions_per_action(self, w):
        f = manual(w)
        for action, actor in (("acknowledge", "viewer"), ("start", "viewer"), ("resolve", "raiser"), ("cancel", "triager")):
            with pytest.raises(PermissionDenied):
                services.transition(f, action, actor=w[actor], reason="x")
        services.transition(f, "resolve", actor=w["triager"], reason="done")
        for action, actor in (("close", "triager"), ("reopen", "triager")):
            with pytest.raises(PermissionDenied):
                services.transition(f, action, actor=w[actor], reason="x")
        with pytest.raises(PermissionDenied):
            services.transition(f, "acknowledge", actor=w["r2_viewer"])  # out of scope

    def test_reasons_are_required_where_the_matrix_says(self, w):
        f = manual(w)
        with pytest.raises(services.FindingError) as exc:
            services.transition(f, "resolve", actor=w["triager"], reason=" ")
        assert exc.value.finding_code == "reason_required"
        with pytest.raises(services.FindingError):
            services.transition(f, "cancel", actor=w["editor"], reason="")
        with pytest.raises(services.FindingError) as exc:
            services.transition(f, "cancel", actor=w["editor"], reason="x" * 301)
        assert exc.value.finding_code == "too_long"
        services.transition(f, "resolve", actor=w["triager"], reason="ok")
        with pytest.raises(services.FindingError):
            services.transition(f, "reopen", actor=w["editor"])

    def test_work_cannot_start_without_an_owner_when_the_severity_requires_one(self, w):
        f = manual(w, severity=sev(w, "high"), assigned_to=w["triager"])
        services.assign(f, actor=w["editor"], assigned_to=None)
        with pytest.raises(services.FindingError) as exc:
            services.transition(f, "start", actor=w["triager"])
        assert exc.value.finding_code == "owner_required"

    def test_reopen_keeps_the_previous_resolution_in_history_and_counts(self, w):
        f = manual(w)
        services.transition(f, "resolve", actor=w["triager"], reason="First fix.")
        services.transition(f, "close", actor=w["editor"])
        f = services.transition(f, "reopen", actor=w["editor"], reason="It cracked again.")
        assert (f.status, f.reopen_count, f.closed_at, f.resolved_at) == ("reopened", 1, None, None)
        assert ActivityLog.objects.get(verb="finding.reopened", metadata__finding_id=str(f.pk)).metadata["previous_resolution"] == "First fix."
        assert f.history.count() >= 4  # django-simple-history keeps every state

    def test_cancelled_is_final(self, w):
        f = manual(w)
        services.transition(f, "cancel", actor=w["editor"], reason="Raised in error")
        for action in ("acknowledge", "start", "resolve", "close", "reopen"):
            with pytest.raises(services.FindingError):
                services.transition(f, action, actor=w["editor"], reason="x")

    def test_a_refusal_is_audited(self, w):
        f = manual(w)
        with pytest.raises(services.FindingError):
            services.transition(f, "close", actor=w["editor"])
        assert ActivityLog.objects.filter(verb="finding.transition_refused", metadata__code="invalid_transition").exists()


class TestClosurePolicy:
    def test_resolved_is_not_verified_and_a_blocker_stops_closing(self, w):
        f = manual(w)
        services.transition(f, "resolve", actor=w["triager"], reason="Fixed")
        blocker = lambda finding: "2 corrective actions are still open"  # noqa: E731
        hooks.register_closure_blocker(blocker)
        try:
            with pytest.raises(services.FindingError) as exc:
                services.transition(f, "close", actor=w["editor"])
            assert exc.value.finding_code == "closure_blocked" and "corrective actions" in str(exc.value.messages[0])
            assert Finding.objects.get(pk=f.pk).status == "resolved"
        finally:
            hooks.closure_blockers.remove(blocker)
        assert services.transition(f, "close", actor=w["editor"]).status == "closed"

    def test_required_verification_is_never_faked(self, w, settings):
        settings.FINDINGS_REQUIRE_VERIFICATION = True
        f = manual(w)
        services.transition(f, "resolve", actor=w["triager"], reason="Fixed")
        with pytest.raises(services.FindingError) as exc:
            services.transition(f, "close", actor=w["editor"])
        assert exc.value.finding_code == "closure_blocked" and "verification" in str(exc.value.messages[0])
        gate = lambda finding: True  # noqa: E731 - what a later verification phase would register
        hooks.register_verification_gate(gate)
        try:
            assert services.transition(f, "close", actor=w["editor"]).status == "closed"
        finally:
            hooks.verification_gate.remove(gate)


class TestConcurrency:
    @pytest.mark.django_db(transaction=True)
    def test_two_concurrent_closes_close_once(self, w):
        f = manual(w)
        services.transition(f, "resolve", actor=w["triager"], reason="Fixed")
        outcomes = []

        def run():
            try:
                services.transition(f, "close", actor=w["editor"])
                outcomes.append("closed")
            except services.FindingConflict:
                outcomes.append("conflict")
            except Exception as exc:  # noqa: BLE001
                outcomes.append(repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(outcomes) == ["closed", "conflict", "conflict"]
        assert ActivityLog.objects.filter(verb="finding.closed", metadata__finding_id=str(f.pk)).count() == 1

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_manual_creation_numbers_are_unique(self, w):
        numbers, errors = [], []

        def run():
            try:
                numbers.append(manual(w).number)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors and sorted(numbers) == [1, 2, 3, 4]


class TestConfiguration:
    def test_defaults_are_seeded_once_per_organization(self, w):
        services.ensure_defaults(w["org_a"])
        assert FindingCategory.objects.filter(organization=w["org_a"]).count() == 6 and FindingSeverity.objects.filter(organization=w["org_a"]).count() == 4
        assert FindingSeverity.objects.get(organization=w["org_a"], is_default=True).code == "medium"

    def test_managing_requires_the_permission_and_stays_in_the_organization(self, w, org_b):
        with pytest.raises(PermissionDenied):
            services.save_category(actor=w["editor"], organization=w["org_a"], code="x", name="X")
        with pytest.raises(PermissionDenied):
            services.save_category(actor=w["admin"], organization=org_b, code="x", name="X")  # admin of Org A only
        c = services.save_category(actor=w["admin"], organization=w["org_a"], code="hygiene", name="Hygiene")
        assert c.code == "hygiene" and not c.is_system
        with pytest.raises(services.FindingError) as exc:
            services.save_category(actor=w["admin"], organization=w["org_a"], category=c, code="renamed", name="Hygiene")
        assert exc.value.finding_code == "code_immutable"
        with pytest.raises(services.FindingConflict):
            services.save_category(actor=w["admin"], organization=w["org_a"], code="hygiene", name="Again")

    def test_severity_rules_rank_order_default_and_validation(self, w):
        s = services.save_severity(actor=w["admin"], organization=w["org_a"], code="blocker", name="Blocker", rank=50, is_escalation=True)
        assert s.rank == 50 and s.tone == "danger"
        with pytest.raises(services.FindingConflict):
            services.save_severity(actor=w["admin"], organization=w["org_a"], code="other", name="Other", rank=50)
        for bad in (0, 101, True):
            with pytest.raises(services.FindingError):
                services.save_severity(actor=w["admin"], organization=w["org_a"], code="r" + str(int(bad)), name="R", rank=bad)
        with pytest.raises(services.FindingError) as exc:
            services.save_severity(actor=w["admin"], organization=w["org_a"], severity=sev(w, "medium"), name="Medium", rank=20, is_default=True, is_active=False)
        assert exc.value.finding_code == "default_required"
        services.save_severity(actor=w["admin"], organization=w["org_a"], severity=s, name="Blocker", rank=50, is_default=True)
        assert FindingSeverity.objects.filter(organization=w["org_a"], is_default=True).count() == 1 and sev(w, "medium").is_default is False

    def test_severity_is_compared_by_rank_never_by_name(self, w):
        services.save_severity(actor=w["admin"], organization=w["org_a"], code="aaa", name="AAA (alphabetically first)", rank=99)
        order = [s.code for s in FindingSeverity.objects.filter(organization=w["org_a"]).order_by("-rank")]
        assert order[0] == "aaa" and order[-1] == "low"


def _state(status):
    now = timezone.now()
    return {
        "open": {"status": "open"}, "acknowledged": {"status": "acknowledged"}, "in_progress": {"status": "in_progress"}, "reopened": {"status": "reopened"},
        "resolved": {"status": "resolved", "resolved_at": now}, "closed": {"status": "closed", "resolved_at": now, "closed_at": now},
        "cancelled": {"status": "cancelled", "cancelled_at": now, "cancel_reason": "x"},
    }[status]
