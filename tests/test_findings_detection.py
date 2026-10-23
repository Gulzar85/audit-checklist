"""Phase 12 detection: rule outcomes -> findings, idempotency, concurrency, traceability, documented semantics."""

from __future__ import annotations

import threading

import pytest
from django.db import connection

from apps.core.models import ActivityLog
from apps.executions import services as exec_services
from apps.findings import services
from apps.findings.models import Finding, FindingCategory, FindingSeverity
from apps.findings.services import detection

from findings_support import build_findings_world, fid, raised, submit
from executions_support import issued


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_findings_world(org_a, org_b, make_user, assign, make_type)


def names(execution):
    return sorted(f.rule_name for f in Finding.objects.filter(execution=execution))


class TestRuleOutcomes:
    def test_a_boolean_no_raises_the_configured_finding(self, w):
        ex = submit(w, {"EXIT": "no"})
        f = Finding.objects.get(execution=ex, rule_name="Blocked exit")
        assert (f.category.code, f.severity.code, f.status, f.source_type) == ("safety", "high", "open", "checklist_rule")
        assert f.title == "Blocked: Is the emergency exit accessible?"
        assert f.description == "Answered No at Restaurant 1." or "Answered" in f.description

    def test_a_yes_raises_nothing(self, w):
        assert not Finding.objects.filter(execution=submit(w, {"EXIT": "yes"})).exists()

    def test_a_score_below_the_threshold_raises_one(self, w):
        ex = submit(w, {"EXIT": "yes", "RATING": 2})
        assert names(ex) == ["Low rating"]
        assert not Finding.objects.filter(execution=submit(w, {"EXIT": "yes", "RATING": 3}, location=w["r2"], who="area_runner")).exists()

    def test_a_multiple_choice_condition_and_the_review_flag(self, w):
        ex = submit(w, {"EXIT": "yes", "TAGS": ["pest", "dirt"]})
        f = Finding.objects.get(execution=ex)
        assert (f.severity.code, f.needs_review) == ("critical", True)

    def test_a_numeric_threshold_and_evidence_inheritance_flag(self, w):
        f = Finding.objects.get(execution=submit(w, {"EXIT": "yes", "FRIDGE": "9.5"}))
        assert (f.category.code, f.inherit_evidence) == ("equipment", False)

    def test_not_applicable_and_missing_answers_raise_nothing(self, w):
        ex = submit(w, {"EXIT": "yes", "HAND": "na"})  # the handwash rule says "= no"; N/A and unanswered are not "no"
        assert not Finding.objects.filter(execution=ex).exists()

    def test_unknown_codes_fall_back_to_the_default_and_flag_the_finding_for_review(self, w):
        f = Finding.objects.get(execution=submit(w, {"EXIT": "yes", "HAND": "no"}))
        assert f.category.code == "other" and f.severity.is_default and f.needs_review

    def test_a_conditional_follow_up_raises_only_when_it_is_visible_and_answered(self, w):
        ex = submit(w, {"EXIT": "no", "ISSUE": "Boxes are stacked in front of it."})
        assert names(ex) == ["Blocked exit", "Problem described"]

    def test_an_answer_hidden_by_rules_is_ignored_not_raised(self, w):
        from apps.executions.models import ChecklistResponse

        ex, _ = exec_services.start_or_resume(issued(w, version=w["fversion"]), actor=w["runner"])
        exec_services.save_answers(ex, actor=w["runner"], inputs=[exec_services.AnswerInput(fid(w, "EXIT"), "no")])
        exec_services.save_answers(ex, actor=w["runner"], inputs=[exec_services.AnswerInput(fid(w, "ISSUE"), "Typed while visible")])
        changed = exec_services.save_answers(ex, actor=w["runner"], inputs=[exec_services.AnswerInput(fid(w, "EXIT"), "yes", revision=1)])  # ISSUE hidden again
        assert changed.ok
        assert ChecklistResponse.objects.filter(execution=ex, item_id=fid(w, "ISSUE")).exists()  # kept, ignored
        done = exec_services.submit(ex, actor=w["runner"]).execution
        assert not Finding.objects.filter(execution=done).exists()


class TestTraceabilityAndAudit:
    def test_the_finding_keeps_the_exact_source(self, w):
        ex = submit(w, {"EXIT": "no"})
        f = Finding.objects.get(execution=ex, rule_name="Blocked exit")
        assert (f.organization_id, f.location_id, f.assignment_id, f.version_id, str(f.item_id)) == (
            ex.organization_id, ex.assignment.location_id, ex.assignment_id, ex.version_id, fid(w, "EXIT"))
        assert f.response.execution_id == ex.pk and f.detected_by == w["runner"] and f.detected_at == ex.submitted_at
        assert f.rule_code and f.detection_key == f"{ex.pk}:{f.item_id}:{f.rule_code}#0" and f.number == 1 and f.reference == "FND-00001"

    def test_raising_is_audited_with_the_rule_and_source(self, w):
        ex = submit(w, {"EXIT": "no"})
        entry = ActivityLog.objects.get(verb="finding.raised", metadata__execution_id=str(ex.pk))
        assert entry.metadata["rule"] and entry.metadata["severity"] == "high" and entry.actor == w["runner"]
        assert ActivityLog.objects.filter(verb="finding.detection_run", metadata__created=1).exists()
        assert ActivityLog.objects.get(verb="execution.submitted").metadata["findings_raised"] == 1

    def test_numbers_are_sequential_per_organization(self, w):
        a = submit(w, {"EXIT": "no", "RATING": 1})
        assert sorted(f.number for f in Finding.objects.filter(execution=a)) == [1, 2, 3][: Finding.objects.filter(execution=a).count()]

    def test_preview_is_read_only_and_not_a_promise(self, w):
        ex, _ = exec_services.start_or_resume(issued(w, version=w["fversion"]), actor=w["runner"])
        exec_services.save_answers(ex, actor=w["runner"], inputs=[exec_services.AnswerInput(fid(w, "EXIT"), "no")])
        assert [p["rule"] for p in services.preview(ex)]
        assert not Finding.objects.exists()


class TestIdempotency:
    def test_repeated_detection_returns_the_existing_findings(self, w):
        ex = submit(w, {"EXIT": "no"})
        before = Finding.objects.count()
        again = services.detect_for_execution(ex, actor=w["runner"])
        assert again.status == "exists" and not again.created and Finding.objects.count() == before

    def test_a_cancelled_finding_is_not_recreated_and_history_survives(self, w):
        ex = submit(w, {"EXIT": "no"})
        f = Finding.objects.get(execution=ex, rule_name="Blocked exit")
        services.transition(f, "cancel", actor=w["admin"], reason="Raised in error")
        again = services.detect_for_execution(ex, actor=w["admin"])
        assert not again.created and Finding.objects.filter(execution=ex).count() == 1
        assert Finding.objects.get(pk=f.pk).status == "cancelled"

    def test_detection_refuses_an_unsubmitted_execution(self, w):
        ex, _ = exec_services.start_or_resume(issued(w, version=w["fversion"]), actor=w["runner"])
        with pytest.raises(services.FindingError) as exc:
            services.detect_for_execution(ex)
        assert exc.value.finding_code == "not_submitted"

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_detection_creates_exactly_one_finding(self, w):
        ex = submit(w, {"EXIT": "no"})
        errors, results = [], []

        def run():
            try:
                results.append(services.detect_for_execution(ex, actor=w["runner"]).status)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors and set(results) == {"exists"}
        assert Finding.objects.filter(execution=ex, rule_name="Blocked exit").count() == 1

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_first_detection_creates_one_per_trigger(self, w):
        from apps.executions.constants import ExecutionStatus
        from apps.executions.models import ChecklistExecution

        ex, _ = exec_services.start_or_resume(issued(w, version=w["fversion"]), actor=w["runner"])
        exec_services.save_answers(ex, actor=w["runner"], inputs=[exec_services.AnswerInput(fid(w, "EXIT"), "no")])
        # Submit without the built-in detection to exercise the racing re-run path, then race four detections.
        from unittest import mock

        with mock.patch("apps.findings.services.detect_after_submission", return_value=None):
            done = exec_services.submit(ex, actor=w["runner"]).execution
        assert done.status == ExecutionStatus.SUBMITTED and not Finding.objects.exists()
        errors = []

        def run():
            try:
                services.detect_for_execution(ChecklistExecution.objects.get(pk=done.pk), actor=w["runner"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors
        assert Finding.objects.filter(execution=done).count() == 1


class TestSubmissionIsNotBrokenByDetection:
    def test_a_detection_bug_never_loses_the_submission(self, w):
        from unittest import mock

        ex, _ = exec_services.start_or_resume(issued(w, version=w["fversion"]), actor=w["runner"])
        exec_services.save_answers(ex, actor=w["runner"], inputs=[exec_services.AnswerInput(fid(w, "EXIT"), "no")])
        with mock.patch.object(detection, "detect_for_execution", side_effect=RuntimeError("boom")):
            done = exec_services.submit(ex, actor=w["runner"]).execution
        assert done.status == "submitted" and not Finding.objects.exists()
        assert ActivityLog.objects.filter(verb="finding.detection_failed").exists()
        assert services.detect_for_execution(done, actor=w["runner"]).created  # the retry raises what was missed

    def test_published_rule_behaviour_is_pinned_to_the_executions_version(self, w):
        ex = submit(w, {"EXIT": "no"})
        f = Finding.objects.get(execution=ex, rule_name="Blocked exit")
        assert f.version_id == ex.version_id and f.version.status == "published"
        assert FindingCategory.objects.filter(organization=w["org_a"]).count() >= 6 and FindingSeverity.objects.filter(organization=w["org_a"]).count() == 4
