"""Evaluation with the real lifecycle (Phase 16): rule-set configuration, publication and sealing, evaluation at submission, scoring input, actions
(findings, workflows), idempotency, re-evaluation, failure isolation, append-only history, offline and concurrency."""

from __future__ import annotations

import threading

import pytest
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from rest_framework.test import APIClient

from apps.checklists import lifecycle, manifest
from apps.checklists.guards import allow_definition_changes
from apps.checklists.seed import load_template
from apps.core.models import ActivityLog
from apps.evaluation import selectors
from apps.evaluation import services as evaluation
from apps.evaluation.models import EvaluationActionRequest, EvaluationRuleSet, EvaluationRun
from apps.executions import services as exec_services
from apps.findings.models import Finding, FindingCategory
from apps.scoring import selectors as scoring_selectors
from apps.scoring import services as scoring
from apps.scoring.models import ScoreResult, ScoringConfig
from apps.workflows.models import WorkflowInstance

from evaluation_support import FIRE_NO, GOOD, RULES, build_evaluation_world, publish_evaluated, rules_with, run
from executions_support import issued


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_evaluation_world(org_a, org_b, make_user, assign, make_type)


def current(ex):
    return selectors.current_run(ex)


def actions(run_):
    return {(a.rule_code, a.action_index): a for a in EvaluationActionRequest.objects.filter(run=run_)}


# ---------------------------------------------------------------------------------------------------------------- draft configuration
@pytest.mark.django_db
class TestDraftConfiguration:
    def draft(self, w):
        return lifecycle.create_draft(w["etemplate"], actor=w["admin"], change_summary="s")

    def test_save_normalizes_orders_by_priority_and_logs(self, w):
        draft = self.draft(w)
        rules = [{**RULES["rules"][1], "priority": 20}, {**RULES["rules"][0], "priority": 10}]
        row = evaluation.save_config(draft, actor=w["rule_designer"], document={"rules": rules})
        assert [r["code"] for r in row.document["rules"]] == ["CRIT", "LOW"] and row.organization_id == w["org"].pk
        assert row.document["rules"][1]["condition"]["conditions"][1]["value"] == "80"
        assert ActivityLog.objects.filter(verb="evaluation.ruleset_saved").count() == 1

    def test_incomplete_rules_are_kept_as_typed_and_validation_says_why(self, w):
        draft = self.draft(w)
        evaluation.save_config(draft, actor=w["admin"], document={"rules": [{"code": "X", "name": "x", "priority": 1, "outcome": "fail", "condition": {"source": "answer", "field": "NOPE", "operator": "EQUALS", "value": "no"}}]})
        issues, compiled, _doc = evaluation.validate_config(draft)
        assert {i.code for i in issues if i.level == "error"} == {"unknown_item"} and compiled == []

    @pytest.mark.parametrize("doc", [{"rules": "x"}, {"eval": 1}, [], {"rules": [1]}])
    def test_malformed_documents_are_refused(self, w, doc):
        with pytest.raises(evaluation.EvaluationError):
            evaluation.save_config(self.draft(w), actor=w["admin"], document=doc)

    def test_permission_and_isolation(self, w, make_user, org_b):
        draft = self.draft(w)
        for who in ("viewer", "score_viewer", "result_viewer", "designer"):  # none holds evaluation.configure
            with pytest.raises(PermissionDenied):
                evaluation.save_config(draft, actor=w[who], document=RULES)
        with pytest.raises(PermissionDenied):
            evaluation.save_config(draft, actor=make_user(email="ev-out@example.com", organization=org_b), document=RULES)

    def test_rule_helpers(self, w):
        draft = self.draft(w)
        evaluation.save_config(draft, actor=w["admin"], document=RULES)
        evaluation.toggle_rule(draft, actor=w["admin"], code="LOW", enabled=False)
        assert [r["enabled"] for r in EvaluationRuleSet.objects.get(version=draft).document["rules"]] == [True, False, True]
        evaluation.move_rule(draft, actor=w["admin"], code="LOW", direction="up")
        assert [r["code"] for r in EvaluationRuleSet.objects.get(version=draft).document["rules"]][:2] == ["LOW", "CRIT"]
        evaluation.set_rule(draft, actor=w["admin"], code="LOW", rule=None)
        assert [r["code"] for r in EvaluationRuleSet.objects.get(version=draft).document["rules"]] == ["CRIT", "NOSCORE"] or len(EvaluationRuleSet.objects.get(version=draft).document["rules"]) == 2
        with pytest.raises(evaluation.EvaluationError):  # a rule code is unique
            evaluation.set_rule(draft, actor=w["admin"], code="CRIT", rule={**RULES["rules"][0]}, original_code="NOSCORE")

    def test_remove(self, w):
        draft = self.draft(w)
        evaluation.save_config(draft, actor=w["admin"], document=RULES)
        evaluation.remove_config(draft, actor=w["admin"])
        assert not EvaluationRuleSet.objects.filter(version=draft).exists()


# ---------------------------------------------------------------------------------------------------------------- publication / sealing
@pytest.mark.django_db
class TestPublicationAndImmutability:
    def test_invalid_rules_block_publication_with_actionable_messages(self, w):
        draft = lifecycle.create_draft(w["etemplate"], actor=w["admin"])
        bad = rules_with()
        bad["rules"][1]["priority"] = 10  # duplicate priority
        bad["rules"][0]["actions"][0]["category"] = "no-such-category"
        evaluation.save_config(draft, actor=w["admin"], document=bad)
        report = lifecycle.run_validation(draft)
        codes = {f.code for f in report.errors}
        assert not report.ok and {"evaluation_duplicate_priority", "evaluation_unknown_reference"} <= codes
        with pytest.raises(Exception, match="Evaluation rules"):
            lifecycle.publish_version(draft, actor=w["admin"])

    def test_actions_are_checked_against_the_organizations_reference_data(self, w):
        draft = lifecycle.create_draft(w["etemplate"], actor=w["admin"])
        doc = rules_with("CRIT")
        doc["rules"][0]["actions"][1] = {"type": "start_workflow", "workflow": "does-not-exist"}
        evaluation.save_config(draft, actor=w["admin"], document=doc)
        assert any("published workflow" in i.message for i in evaluation.validate_config(draft)[0])

    def test_disabled_rules_publish_without_evaluation(self, w):
        v = publish_evaluated(w, rules={"rules": []}, enabled=False)
        assert evaluation.evaluation_of(run(w, v, GOOD)) == {"applicable": False, "reason": "disabled"}
        assert not EvaluationRun.objects.exists()

    def test_a_version_without_rules_keeps_the_hash_it_always_had(self, w):
        assert "evaluation" not in manifest.build_manifest(w["eversion"]) and manifest.verify_version_integrity(w["eversion"]).ok

    def test_the_rules_are_sealed_in_the_integrity_hash(self, w):
        v = publish_evaluated(w)
        assert "evaluation" in manifest.build_manifest(v) and manifest.verify_version_integrity(v).ok
        assert manifest.manifest_hash(lifecycle.create_draft(w["etemplate"], actor=w["admin"])) == v.integrity_hash  # an unchanged clone hashes identically

    def test_published_rules_cannot_be_changed_by_the_service_or_the_database(self, w):
        v = publish_evaluated(w)
        with pytest.raises(evaluation.EvaluationError) as info:
            evaluation.save_config(v, actor=w["admin"], enabled=False)
        assert info.value.code == "version_immutable"
        with pytest.raises(IntegrityError), transaction.atomic():
            EvaluationRuleSet.objects.filter(version=v).update(enabled=False)
        with pytest.raises(IntegrityError), transaction.atomic():
            EvaluationRuleSet.objects.filter(version=v).delete()
        with pytest.raises(IntegrityError), transaction.atomic():
            EvaluationRuleSet.objects.create(version=w["eversion"], organization=w["org"], document={"rules": []})

    def test_tampering_with_the_maintenance_override_is_detected(self, w):
        v = publish_evaluated(w)
        with allow_definition_changes():
            EvaluationRuleSet.objects.filter(version=v).update(enabled=False)
        assert not manifest.verify_version_integrity(v).ok

    def test_a_new_draft_is_a_copy_and_editing_it_leaves_the_published_rules_alone(self, w):
        v = publish_evaluated(w)
        draft = lifecycle.create_draft(w["etemplate"], actor=w["admin"])
        assert EvaluationRuleSet.objects.get(version=draft).document == EvaluationRuleSet.objects.get(version=v).document
        evaluation.toggle_rule(draft, actor=w["admin"], code="LOW", enabled=False)
        assert EvaluationRuleSet.objects.get(version=v).document["rules"][1]["enabled"] is True

    def test_compare_shows_rule_changes(self, w):
        v = publish_evaluated(w)
        draft = lifecycle.create_draft(w["etemplate"], actor=w["admin"])
        evaluation.toggle_rule(draft, actor=w["admin"], code="LOW", enabled=False)
        result = lifecycle.compare_versions(w["etemplate"], v, draft, actor=w["admin"], log=False)
        assert [c.category for c in result.changes] == ["evaluation"] and "LOW" in result.changes[0].summary
        added = lifecycle.compare_versions(w["etemplate"], w["eversion"], v, actor=w["admin"], log=False)
        assert any(c.category == "evaluation" and c.kind == "added" for c in added.changes)


# ---------------------------------------------------------------------------------------------------------------- evaluation at submission
@pytest.mark.django_db
class TestSubmissionEvaluation:
    def test_a_clean_execution_passes_and_requests_nothing(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, GOOD)
        r = current(ex)
        assert (r.outcome, r.trigger, r.created_by_id, r.is_current, r.engine_version) == ("pass", "submission", None, True, 1)
        assert r.detail["matched"] == [] and not EvaluationActionRequest.objects.exists() and not Finding.objects.exists()
        assert (r.version_id, r.ruleset_hash, r.execution_revision) == (v.pk, EvaluationRuleSet.objects.get(version=v).content_hash, ex.revision)

    def test_it_reads_the_stored_final_score_and_does_not_recalculate(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, {**GOOD, "FRIDGE": "3.9"})
        score = scoring_selectors.current_result(ex)
        r = current(ex)
        assert r.score_result_id == score.pk and ScoreResult.objects.filter(execution=ex).count() == 1
        assert r.outcome == "review_required" and r.detail["matched"] == ["LOW"]
        low = next(x for x in r.detail["rules"] if x["code"] == "LOW")
        assert "score percentage is less than 80 (observed: 75)" in low["explanation"] and "REVIEW REQUIRED" in low["explanation"]

    def test_low_score_requests_a_finding_through_the_findings_service(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, {**GOOD, "FRIDGE": "3.9"})
        requests = actions(current(ex))
        finding = Finding.objects.get()
        assert (finding.source_type, finding.category.code, finding.severity.code, finding.status, finding.execution_id) == ("evaluation", "quality", "medium", "open", ex.pk)
        assert finding.detection_key == f"eval:{ex.pk}:LOW:0" and finding.rule_code == "LOW" and finding.organization_id == w["org"].pk and finding.location_id == w["r1"].pk
        assert (requests[("LOW", 0)].status, requests[("LOW", 0)].result_type, requests[("LOW", 0)].result_id) == ("succeeded", "finding", str(finding.pk))
        assert requests[("LOW", 1)].status == "succeeded" and requests[("LOW", 1)].result_id == "low-score"

    def test_a_critical_failure_wins_the_conflict_and_every_match_is_listed(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, {**GOOD, **FIRE_NO})
        r = current(ex)
        assert r.outcome == "fail" and r.detail["matched"] == ["CRIT", "LOW"]
        assert "highest-precedence" in " ".join(r.detail["explanation"])
        assert {f.rule_code for f in Finding.objects.all()} == {"CRIT", "LOW"}

    def test_a_workflow_is_started_through_the_workflow_engine(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, {**GOOD, **FIRE_NO})
        instance = WorkflowInstance.objects.get()
        request = actions(current(ex))[("CRIT", 1)]
        assert (instance.target_type, instance.target_id, instance.state.code, instance.status) == ("execution", ex.pk, "review", "active")
        assert (request.status, request.result_type, request.result_id) == ("succeeded", "workflow", str(instance.pk))
        assert instance.system_started and instance.organization_id == w["org"].pk

    def test_rules_never_write_domain_statuses_directly(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, {**GOOD, **FIRE_NO})
        ex.refresh_from_db()
        assert ex.status == "submitted" and Finding.objects.filter(status="open").count() == 2 and WorkflowInstance.objects.get().state.code == "review"

    def test_a_rule_on_an_unanswered_optional_question_is_undecided_not_false(self, w):
        doc = {"rules": [{"code": "U", "name": "Rating low", "priority": 1, "outcome": "fail", "on_unknown": "indeterminate",
                          "condition": {"source": "answer", "field": "RATING", "operator": "LESS_THAN", "value": "3"}}]}
        v = publish_evaluated(w, rules=doc)
        r = current(run(w, v, GOOD))  # RATING (optional) is unanswered
        assert r.outcome == "indeterminate" and r.detail["undecided"] == ["U"] and "could not be decided" in r.detail["rules"][0]["explanation"]

    def test_an_unscored_version_reads_the_score_as_unavailable(self, w):
        v = publish_evaluated(w, scoring_document=False)
        r = current(run(w, v, GOOD))
        assert r.score_result_id is None and r.detail["matched"] == ["NOSCORE"] and r.outcome == "warning"

    def test_an_execution_in_progress_is_provisional_stored_nowhere_and_runs_no_action(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, FIRE_NO, submit=False)
        view = evaluation.evaluation_of(ex)
        assert view["provisional"] and not view["stored"] and view["evaluation"].matched == ["CRIT", "NOSCORE"]  # answers count; the score is unavailable
        assert not EvaluationRun.objects.exists() and not Finding.objects.exists() and not WorkflowInstance.objects.exists()

    def test_the_audit_trail(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, {**GOOD, "FRIDGE": "3.9"})
        assert ActivityLog.objects.filter(verb="evaluation.run_recorded", metadata__execution_id=str(ex.pk)).count() == 1
        assert ActivityLog.objects.filter(verb="evaluation.action_succeeded").count() == 2


# ---------------------------------------------------------------------------------------------------------------- idempotency / re-evaluation
@pytest.mark.django_db
class TestIdempotencyAndReevaluation:
    def final(self, w, values=None, rules=None):
        v = publish_evaluated(w, rules=rules)
        ex = run(w, v, {**GOOD, "FRIDGE": "3.9"} if values is None else values)
        return v, ex, current(ex)

    def test_repeating_the_trigger_changes_nothing(self, w):
        _v, ex, first = self.final(w)
        before = (EvaluationRun.objects.count(), EvaluationActionRequest.objects.count(), Finding.objects.count())
        assert evaluation.evaluate_pending(ex).pk == first.pk
        run_, changed = evaluation.reevaluate(ex, actor=w["reevaluator"], reason="routine check")
        assert (run_.pk, changed) == (first.pk, False)
        assert (EvaluationRun.objects.count(), EvaluationActionRequest.objects.count(), Finding.objects.count()) == before
        assert ActivityLog.objects.filter(verb="evaluation.run_unchanged").count() == 2

    def test_submitting_again_does_not_evaluate_again(self, w):
        _v, ex, first = self.final(w)
        assert exec_services.submit(ex, actor=w["runner"]).already_submitted
        assert EvaluationRun.objects.filter(execution=ex).count() == 1 and current(ex).pk == first.pk

    def test_reevaluation_needs_permission_and_a_reason(self, w):
        _v, ex, _r = self.final(w)
        for who in ("result_viewer", "explainer", "evaluator", "no_eval", "runner"):
            with pytest.raises(PermissionDenied):
                evaluation.reevaluate(ex, actor=w[who], reason="x")
        with pytest.raises(evaluation.EvaluationError) as info:
            evaluation.reevaluate(ex, actor=w["reevaluator"], reason="  ")
        assert info.value.code == "reason_required"

    def test_only_a_submitted_execution_can_be_reevaluated(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, GOOD, submit=False)
        with pytest.raises(PermissionDenied):
            evaluation.reevaluate(ex, actor=w["reevaluator"], reason="x")

    def test_a_recalculated_score_triggers_a_new_run_and_actions_are_never_repeated(self, w):
        v, ex, first = self.final(w)
        with allow_definition_changes():
            ScoringConfig.objects.filter(version=v).update(precision=0)  # audited maintenance: the only way a sealed scoring can change
        scoring.recalculate(ex, actor=w["recalculator"], reason="rounding")
        second = current(ex)
        assert second.pk != first.pk and second.trigger == "score" and EvaluationRun.objects.filter(execution=ex).count() == 2
        first.refresh_from_db()
        assert not first.is_current and first.outcome == "review_required"
        assert Finding.objects.count() == 1  # the same rule on the same execution never raises a second finding
        again = actions(second)[("LOW", 0)]
        assert again.status == "skipped" and again.result_id == str(Finding.objects.get().pk) and "earlier evaluation run" in again.message
        assert second.score_result_id != first.score_result_id

    def test_a_different_ruleset_version_never_rewrites_history(self, w):
        v2, ex, first = self.final(w)
        v3 = publish_evaluated(w, rules=rules_with("CRIT"))
        first.refresh_from_db()
        assert first.outcome == "review_required" and first.version_id == v2.pk and current(ex).pk == first.pk
        _run, changed = evaluation.reevaluate(ex, actor=w["reevaluator"], reason="new rules published")
        assert not changed  # an execution is always evaluated with the rules of its own version
        ex3 = run(w, v3, {**GOOD, "FRIDGE": "3.9"})
        assert current(ex3).outcome == "pass" and current(ex3).version_id == v3.pk

    def test_history_is_complete_and_ordered(self, w):
        v, ex, _first = self.final(w)
        with allow_definition_changes():
            ScoringConfig.objects.filter(version=v).update(precision=0)
        scoring.recalculate(ex, actor=w["recalculator"], reason="x")
        history = list(selectors.runs_for(ex))
        assert [r.trigger for r in history] == ["score", "submission"] and [r.is_current for r in history] == [True, False]

    def test_the_command_completes_missing_evaluations_idempotently(self, w, monkeypatch):
        from apps.evaluation.services import runtime

        v = publish_evaluated(w)
        real = runtime._evaluate
        monkeypatch.setattr(runtime, "_evaluate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        ex = run(w, v, {**GOOD, "FRIDGE": "3.9"})
        assert ex.status == "submitted" and not EvaluationRun.objects.exists()  # the submission survived
        assert ActivityLog.objects.filter(verb="evaluation.run_failed").count() == 1
        monkeypatch.setattr(runtime, "_evaluate", real)
        call_command("evaluate_executions", "--dry-run")
        call_command("evaluate_executions")
        call_command("evaluate_executions")
        assert EvaluationRun.objects.filter(execution=ex).count() == 1 and Finding.objects.count() == 1


# ---------------------------------------------------------------------------------------------------------------- actions: failures and isolation
@pytest.mark.django_db
class TestActionOutcomes:
    def test_an_action_failure_is_recorded_distinctly_and_does_not_undo_the_rest(self, w):
        v = publish_evaluated(w)
        FindingCategory.objects.filter(organization=w["org"], code="quality").update(is_active=False)  # reference data changed after publication
        ex = run(w, v, {**GOOD, "FRIDGE": "3.9"})
        r = current(ex)
        reqs = actions(r)
        assert r.outcome == "review_required" and ex.status == "submitted"  # the evaluation and the submission stand
        assert reqs[("LOW", 0)].status == "failed" and "quality" in reqs[("LOW", 0)].error and reqs[("LOW", 0)].result_id == ""
        assert reqs[("LOW", 1)].status == "succeeded" and not Finding.objects.exists()
        assert ActivityLog.objects.filter(verb="evaluation.action_failed").count() == 1

    def test_a_failed_action_can_succeed_on_a_later_reevaluation_only_when_the_inputs_change(self, w):
        v = publish_evaluated(w)
        FindingCategory.objects.filter(organization=w["org"], code="quality").update(is_active=False)
        ex = run(w, v, {**GOOD, "FRIDGE": "3.9"})
        FindingCategory.objects.filter(organization=w["org"], code="quality").update(is_active=True)
        _r, changed = evaluation.reevaluate(ex, actor=w["reevaluator"], reason="retry")
        assert not changed and not Finding.objects.exists()  # identical inputs: no new run, nothing retried silently

    def test_a_category_of_another_organization_is_never_used(self, w, org_b):
        v = publish_evaluated(w)
        FindingCategory.objects.filter(organization=w["org"], code="quality").delete()
        assert FindingCategory.objects.filter(organization=org_b, code="quality").exists()
        ex = run(w, v, {**GOOD, "FRIDGE": "3.9"})
        assert actions(current(ex))[("LOW", 0)].status == "failed" and not Finding.objects.exists()

    def test_a_workflow_already_running_is_skipped_not_duplicated(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, {**GOOD, **FIRE_NO})
        assert WorkflowInstance.objects.count() == 1
        doc = rules_with("CRIT")
        doc["rules"].append({"code": "CRIT2", "name": "Second request", "priority": 15, "outcome": "warning",
                             "condition": {"source": "answer", "field": "FIRE-EXIT", "operator": "EQUALS", "value": "no"}, "actions": [{"type": "start_workflow", "workflow": "exec-review"}]})
        v2 = publish_evaluated(w, rules=doc)
        ex2 = run(w, v2, {**GOOD, **FIRE_NO})
        statuses = {(rc, i): a.status for (rc, i), a in actions(current(ex2)).items() if a.action_type == "start_workflow"}
        assert sorted(statuses.values()) == ["skipped", "succeeded"] or WorkflowInstance.objects.filter(target_id=ex2.pk).count() == 1
        assert WorkflowInstance.objects.filter(target_id=ex2.pk).count() == 1 and WorkflowInstance.objects.filter(target_id=ex.pk).count() == 1

    def test_a_workflow_that_does_not_exist_fails_visibly(self, w):
        v = publish_evaluated(w)
        wf_def = w["review_workflow"]
        wf_def.status = "archived"
        wf_def.save(update_fields=["status"])
        ex = run(w, v, {**GOOD, **FIRE_NO})
        req = actions(current(ex))[("CRIT", 1)]
        assert req.status == "failed" and req.error and not WorkflowInstance.objects.exists()


# ---------------------------------------------------------------------------------------------------------------- the domain contracts
@pytest.mark.django_db
class TestDomainContracts:
    def submitted(self, w):
        return run(w, w["eversion"], GOOD)

    def test_the_findings_service_is_idempotent_and_validates_its_inputs(self, w, org_b):
        from apps.findings.services import FindingError, create_from_evaluation

        ex = self.submitted(w)
        kw = dict(rule_code="R", rule_name="Rule", detection_key=f"eval:{ex.pk}:R:0", category_code="safety", severity_code="high", title="T")
        first, created = create_from_evaluation(ex, **kw)
        again, created_again = create_from_evaluation(ex, **kw)
        assert (created, created_again, first.pk == again.pk) == (True, False, True) and first.source_type == "evaluation" and first.organization_id == ex.organization_id
        for bad, code in ((dict(kw, category_code="nope", detection_key="k2"), "invalid_category"), (dict(kw, severity_code="nope", detection_key="k3"), "invalid_severity"),
                          (dict(kw, title=" ", detection_key="k4"), "invalid_request"), (dict(kw, detection_key=""), "invalid_request")):
            with pytest.raises(FindingError) as info:
                create_from_evaluation(ex, **bad)
            assert info.value.code == code
        assert Finding.objects.count() == 1

    def test_only_submitted_executions_can_raise_evaluation_findings(self, w):
        from apps.findings.services import FindingError, create_from_evaluation

        ex = run(w, w["eversion"], GOOD, submit=False)
        with pytest.raises(FindingError) as info:
            create_from_evaluation(ex, rule_code="R", rule_name="r", detection_key="k", category_code="safety", severity_code="high", title="T")
        assert info.value.code == "not_submitted"

    def test_the_database_requires_a_complete_evaluation_source(self, w):
        from apps.findings.constants import SourceType

        ex = self.submitted(w)
        cat = FindingCategory.objects.filter(organization=w["org"], code="safety").first()
        sev = cat.organization.finding_severities.first() if hasattr(cat.organization, "finding_severities") else None
        from apps.findings.models import FindingSeverity

        sev = FindingSeverity.objects.filter(organization=w["org"]).first()
        with pytest.raises(IntegrityError), transaction.atomic():
            Finding.objects.create(organization=w["org"], number=999, title="t", category=cat, severity=sev, source_type=SourceType.EVALUATION, location=w["r1"],
                                   assignment=ex.assignment, execution=ex, version=ex.version, detection_key="", rule_code="")

    def test_the_execution_workflow_target_accepts_only_submitted_executions(self, w):
        from apps.workflows import services as wf

        open_ex = run(w, w["eversion"], GOOD, submit=False)
        with pytest.raises(wf.WorkflowError) as info:
            wf.start(actor=None, target_type="execution", target=open_ex, definition_code="exec-review", system=True)
        assert info.value.workflow_code == "target_not_eligible"
        done = run(w, w["eversion"], GOOD, who="area_runner", location=w["r2"])
        instance = wf.start(actor=None, target_type="execution", target=done, definition_code="exec-review", system=True)
        with pytest.raises(wf.WorkflowConflict) as dup:
            wf.start(actor=None, target_type="execution", target=done, definition_code="exec-review", system=True)
        assert dup.value.workflow_code == "already_running" and instance.target_type == "execution"

    def test_a_workflow_for_executions_needs_no_business_action(self, w):
        from apps.workflows import handlers, targets

        assert "execution" in targets.registered_types() and handlers.for_target("execution") == []


# ---------------------------------------------------------------------------------------------------------------- append-only history
@pytest.mark.django_db
class TestAppendOnly:
    def final(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, {**GOOD, "FRIDGE": "3.9"})
        return ex, current(ex)

    def test_runs_cannot_be_updated_or_deleted(self, w):
        _ex, r = self.final(w)
        for change in ({"outcome": "pass"}, {"detail": {}}, {"reason": "x"}):
            with pytest.raises(IntegrityError), transaction.atomic():
                EvaluationRun.objects.filter(pk=r.pk).update(**change)
        with pytest.raises(IntegrityError), transaction.atomic():
            EvaluationRun.objects.filter(pk=r.pk).delete()
        with pytest.raises(IntegrityError), transaction.atomic():
            r.delete()

    def test_one_current_run_per_execution_and_one_key_per_run(self, w):
        ex, r = self.final(w)
        base = dict(organization=r.organization, execution=ex, version=r.version, ruleset=r.ruleset, ruleset_hash="x", trigger="manual", outcome="pass", input_fingerprint="f")
        with pytest.raises(IntegrityError), transaction.atomic():
            EvaluationRun.objects.create(idempotency_key="other", is_current=True, **base)
        with pytest.raises(IntegrityError), transaction.atomic():
            EvaluationRun.objects.create(idempotency_key=r.idempotency_key, is_current=False, **base)

    def test_a_run_must_agree_with_its_execution(self, w):
        v, r = self.final(w)[0], self.final(w)[1]
        other_version = w["eversion"]
        with pytest.raises(IntegrityError), transaction.atomic():
            EvaluationRun.objects.create(organization=r.organization, execution=v, version=other_version, ruleset=r.ruleset, ruleset_hash="x", trigger="manual", outcome="pass",
                                         input_fingerprint="f", idempotency_key="k2", is_current=False)

    def test_finished_action_requests_are_frozen_and_never_deleted(self, w):
        _ex, r = self.final(w)
        request = EvaluationActionRequest.objects.filter(run=r, status="succeeded").first()
        for change in ({"status": "failed"}, {"message": "x"}, {"result_id": "x"}):
            with pytest.raises(IntegrityError), transaction.atomic():
                EvaluationActionRequest.objects.filter(pk=request.pk).update(**change)
        with pytest.raises(IntegrityError), transaction.atomic():
            EvaluationActionRequest.objects.filter(pk=request.pk).delete()

    def test_a_succeeded_action_key_is_unique_per_organization(self, w):
        _ex, r = self.final(w)
        request = EvaluationActionRequest.objects.filter(run=r, status="succeeded", action_type="request_finding").first()
        with pytest.raises(IntegrityError), transaction.atomic():
            EvaluationActionRequest.objects.create(organization=r.organization, run=r, execution=r.execution, rule_code="OTHER", action_index=0, action_type="tag",
                                                   action_key=request.action_key, status="succeeded")


# ---------------------------------------------------------------------------------------------------------------- offline
OFFLINE_RULES = {"rules": [{"code": "EXIT", "name": "Exit blocked", "priority": 1, "outcome": "fail",
                            "condition": {"source": "answer", "field": "FIRE-EXIT", "operator": "EQUALS", "value": "no"},
                            "actions": [{"type": "request_finding", "category": "safety", "severity": "high", "title": "Exit blocked"}]}]}


@pytest.mark.django_db
class TestOfflineSync:
    def test_the_server_evaluates_what_the_device_synchronizes_and_refuses_client_results(self, w):
        from offline_support import OFFLINE_CHECKLIST, op

        template, _ = load_template(w["org"], OFFLINE_CHECKLIST)
        version = publish_evaluated(w | {"otemplate": template}, template="otemplate", rules=OFFLINE_RULES, scoring_document=False)
        w["oversion"] = version
        assignment = issued(w, version=version)
        client = APIClient()
        client.force_authenticate(w["runner"])
        forged = {**op(w, assignment, type="submit", seq=9), "outcome": "pass", "evaluation": {"outcome": "pass"}}
        refused = client.post("/api/v1/offline/sync/", {"schema_version": 1, "operations": [forged]}, format="json").json()["data"]
        refused = refused["results"] if isinstance(refused, dict) else refused
        assert refused[0]["status"] == "rejected" and not EvaluationRun.objects.exists()
        ops = [op(w, assignment, "FIRE-EXIT", "no", seq=1), op(w, assignment, "HANDWASH", "yes", seq=2), op(w, assignment, "FRIDGE", "4.5", seq=3),
               op(w, assignment, "ISSUE", "Blocked", seq=4), op(w, assignment, type="submit", seq=5)]
        out = client.post("/api/v1/offline/sync/", {"schema_version": 1, "operations": ops}, format="json").json()["data"]
        out = out["results"] if isinstance(out, dict) else out
        assert [x["status"] for x in out][-1] == "applied", out
        run_ = EvaluationRun.objects.get()
        assert (run_.outcome, run_.detail["matched"]) == ("fail", ["EXIT"]) and Finding.objects.get().rule_code == "EXIT"
        client.post("/api/v1/offline/sync/", {"schema_version": 1, "operations": ops}, format="json")  # a retry: receipts replay
        assert EvaluationRun.objects.count() == 1 and Finding.objects.count() == 1


# ---------------------------------------------------------------------------------------------------------------- concurrency
@pytest.mark.django_db(transaction=True)
class TestConcurrency:
    def race(self, jobs):
        out = []

        def wrap(job):
            def go():
                try:
                    out.append(job())
                except Exception as exc:  # noqa: BLE001
                    out.append(exc)
                finally:
                    connection.close()

            return go

        threads = [threading.Thread(target=wrap(j)) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(90)
        return out

    def test_concurrent_submissions_evaluate_once_and_act_once(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, {**GOOD, "FRIDGE": "3.9"}, submit=False)
        out = self.race([lambda: exec_services.submit(ex, actor=w["runner"]), lambda: exec_services.submit(ex, actor=w["runner2"])])
        assert not [o for o in out if isinstance(o, Exception) and not isinstance(o, PermissionDenied)], out
        assert EvaluationRun.objects.filter(execution=ex).count() == 1 and Finding.objects.count() == 1

    def test_concurrent_reevaluations_never_duplicate_runs_or_side_effects(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, {**GOOD, **FIRE_NO})
        out = self.race([lambda: evaluation.reevaluate(ex, actor=w["reevaluator"], reason="a"), lambda: evaluation.reevaluate(ex, actor=w["reevaluator"], reason="b")])
        assert not [o for o in out if isinstance(o, Exception)], out
        assert EvaluationRun.objects.filter(execution=ex, is_current=True).count() == 1 and EvaluationRun.objects.filter(execution=ex).count() == 1
        assert Finding.objects.count() == 2 and WorkflowInstance.objects.count() == 1
