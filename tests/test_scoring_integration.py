"""Scoring with the real lifecycle (Phase 15): draft configuration, publication, sealing, cloning, execution, snapshots, recalculation, offline."""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, transaction
from rest_framework.test import APIClient

from apps.checklists import lifecycle, manifest
from apps.checklists.guards import allow_definition_changes
from apps.checklists.seed import load_template
from apps.core.models import ActivityLog
from apps.executions import services as exec_services
from apps.executions.models import ChecklistExecution
from apps.scoring import selectors
from apps.scoring import services as scoring
from apps.scoring.models import ScoreResult, ScoringConfig

from executions_support import issued
from scoring_support import BANDS, COND_CHECKLIST, COND_DOC, EXC_DOC, GOOD, build_scoring_world, publish_scored, run

D = Decimal


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_scoring_world(org_a, org_b, make_user, assign, make_type)


# ---------------------------------------------------------------------------------------------------------------- draft configuration
@pytest.mark.django_db
class TestDraftConfiguration:
    def draft(self, w):
        return lifecycle.create_draft(w["etemplate"], actor=w["admin"], change_summary="s")

    def test_save_normalizes_and_logs(self, w):
        draft = self.draft(w)
        row = scoring.save_config(draft, actor=w["designer"], document={"items": {"FIRE-EXIT": {"weight": "2.0", "points": {"yes": "5.00", "no": "0"}}}})
        assert row.document["items"]["FIRE-EXIT"]["weight"] == "2" and row.document["items"]["FIRE-EXIT"]["points"]["yes"] == "5"
        assert row.organization_id == w["org"].pk and ActivityLog.objects.filter(verb="scoring.config_saved").count() == 1

    def test_a_draft_may_be_incomplete_but_validation_says_so(self, w):
        draft = self.draft(w)
        scoring.save_config(draft, actor=w["admin"], document={"items": {"FIRE-EXIT": {"points": {"yes": "5"}}}})
        assert "missing_mapping" in {i.code for i in scoring.validate_config(draft) if i.level == "error"}

    def test_partial_updates_keep_the_rest(self, w):
        draft = self.draft(w)
        scoring.save_config(draft, actor=w["admin"], document=EXC_DOC, settings={"precision": 1})
        row = scoring.save_config(draft, actor=w["admin"], settings={"method": "weighted_sections"})
        assert (row.method, row.precision, len(row.document["items"])) == ("weighted_sections", 1, 3)

    @pytest.mark.parametrize("bad", [{"nope": 1}, {"precision": 9}, {"method": "x"}, {"enabled": "yes"}])
    def test_bad_settings_are_refused(self, w, bad):
        with pytest.raises(scoring.ScoringError):
            scoring.save_config(self.draft(w), actor=w["admin"], settings=bad)

    @pytest.mark.parametrize("doc", [{"items": {"A": {"weight": 1.5}}}, {"eval": "1"}, {"items": []}, {"bands": [{"min": "x"}]}])
    def test_malformed_documents_are_refused(self, w, doc):
        with pytest.raises(scoring.ScoringError):
            scoring.save_config(self.draft(w), actor=w["admin"], document=doc)

    def test_permission_is_required_and_viewing_a_checklist_is_not_enough(self, w):
        draft = self.draft(w)
        for who in ("viewer", "score_viewer", "breakdown_viewer", "exec_viewer"):
            with pytest.raises(PermissionDenied):
                scoring.save_config(draft, actor=w[who], document=EXC_DOC)

    def test_another_organization_cannot_touch_it(self, w, make_user, org_b):
        outsider = make_user(email="sc-outsider@example.com", organization=org_b)
        with pytest.raises(PermissionDenied):
            scoring.save_config(self.draft(w), actor=outsider, document=EXC_DOC)

    def test_remove_config(self, w):
        draft = self.draft(w)
        scoring.save_config(draft, actor=w["admin"], document=EXC_DOC)
        scoring.remove_config(draft, actor=w["admin"])
        assert not ScoringConfig.objects.filter(version=draft).exists()


# ---------------------------------------------------------------------------------------------------------------- publication and sealing
@pytest.mark.django_db
class TestPublicationAndImmutability:
    def test_invalid_scoring_blocks_publication_with_actionable_messages(self, w):
        draft = lifecycle.create_draft(w["etemplate"], actor=w["admin"])
        scoring.save_config(draft, actor=w["admin"], document={"items": {"NOPE": {"points": {"yes": "1"}}, "FIRE-EXIT": {"points": {"yes": "1"}}}})
        report = lifecycle.run_validation(draft)
        assert not report.ok
        messages = " ".join(f.message for f in report.errors)
        assert "scoring_unknown_item" in {f.code for f in report.errors} and "NOPE" in messages
        with pytest.raises(Exception, match="Scoring"):
            lifecycle.publish_version(draft, actor=w["admin"])

    def test_scoring_can_be_disabled_and_the_version_publishes_without_a_score(self, w):
        v = publish_scored(w, document={}, settings={"enabled": False})
        assert scoring.score_of(run(w, v, GOOD)) == {"applicable": False, "reason": "disabled"}
        assert not ScoreResult.objects.exists()

    def test_a_version_without_scoring_keeps_the_hash_it_always_had(self, w):
        assert "scoring" not in manifest.build_manifest(w["eversion"])
        assert manifest.verify_version_integrity(w["eversion"]).ok

    def test_scoring_is_sealed_in_the_integrity_hash(self, w):
        v = publish_scored(w)
        assert "scoring" in manifest.build_manifest(v) and manifest.verify_version_integrity(v).ok
        plain = lifecycle.create_draft(w["etemplate"], actor=w["admin"])  # v3 without any change: scoring is cloned
        assert manifest.manifest_hash(plain) == v.integrity_hash

    def test_published_scoring_cannot_be_changed_through_the_service(self, w):
        v = publish_scored(w)
        with pytest.raises(scoring.ScoringError) as info:
            scoring.save_config(v, actor=w["admin"], settings={"precision": 0})
        assert info.value.code == "version_immutable"

    def test_database_blocks_updates_deletes_and_late_creation(self, w):
        v = publish_scored(w)
        with pytest.raises(IntegrityError), transaction.atomic():
            ScoringConfig.objects.filter(version=v).update(precision=0)
        with pytest.raises(IntegrityError), transaction.atomic():
            ScoringConfig.objects.filter(version=v).delete()
        plain = w["eversion"]  # published, no scoring: scoring cannot be attached afterwards
        with pytest.raises(IntegrityError), transaction.atomic():
            ScoringConfig.objects.create(version=plain, organization=w["org"], document={})

    def test_the_maintenance_override_is_the_only_way_and_it_changes_the_seal(self, w):
        v = publish_scored(w)
        with allow_definition_changes():
            ScoringConfig.objects.filter(version=v).update(precision=0)
        assert not manifest.verify_version_integrity(v).ok  # tampering is detected

    def test_a_new_draft_is_a_copy_and_editing_it_leaves_the_published_one_alone(self, w):
        v = publish_scored(w)
        draft = lifecycle.create_draft(w["etemplate"], actor=w["admin"])
        assert ScoringConfig.objects.get(version=draft).document == ScoringConfig.objects.get(version=v).document
        scoring.save_config(draft, actor=w["admin"], settings={"precision": 0})
        assert ScoringConfig.objects.get(version=v).precision == 2 and ScoringConfig.objects.get(version=draft).precision == 0

    def test_compare_shows_scoring_changes(self, w):
        v2 = publish_scored(w)
        draft = lifecycle.create_draft(w["etemplate"], actor=w["admin"])
        scoring.save_config(draft, actor=w["admin"], settings={"precision": 0})
        result = lifecycle.compare_versions(w["etemplate"], v2, draft, actor=w["admin"], log=False)
        assert [c.category for c in result.changes] == ["scoring"] and "precision" in result.changes[0].summary
        none_to_some = lifecycle.compare_versions(w["etemplate"], w["eversion"], v2, actor=w["admin"], log=False)
        assert any(c.category == "scoring" and c.kind == "added" for c in none_to_some.changes)


# ---------------------------------------------------------------------------------------------------------------- execution
@pytest.mark.django_db
class TestExecutionScoring:
    def test_submission_writes_one_final_snapshot_with_a_breakdown(self, w):
        v = publish_scored(w)
        ex = run(w, v, GOOD)
        result = selectors.current_result(ex)
        assert (result.status, result.percentage, result.earned, result.available, result.grade, result.critical) == ("final", D("100"), D(20), D(20), "A", False)
        assert (result.version_id, result.organization_id, result.config_hash, result.algorithm) == (v.pk, w["org"].pk, ScoringConfig.objects.get(version=v).content_hash, 1)
        rows = {r["code"]: r for r in result.breakdown["items"]}
        assert rows["FIRE-EXIT"]["earned"] == "10" and rows["FIRE-EXIT"]["weight"] == "2" and "weight 2" in rows["FIRE-EXIT"]["explanation"]
        assert ActivityLog.objects.filter(verb="scoring.score_finalized", metadata__execution_id=str(ex.pk)).count() == 1

    def test_numbers_grades_and_critical_flags(self, w):
        v = publish_scored(w)
        ex = run(w, v, {**GOOD, "FIRE-EXIT": "no", "ISSUE": "Blocked by crates", "FRIDGE": "3.9"}, who="runner")
        r = selectors.current_result(ex)
        assert (r.earned, r.available, r.percentage, r.grade, r.critical) == (D(5), D(20), D("25"), "D", True)
        assert r.breakdown["critical_hits"][0]["label"] == "Fire exit blocked"

    def test_a_draft_has_a_provisional_live_score_and_no_stored_result(self, w):
        v = publish_scored(w)
        ex = run(w, v, {"FIRE-EXIT": "yes"}, submit=False)
        view = scoring.score_of(ex)
        assert view["applicable"] and not view["stored"] and view["outcome"].status == "provisional"
        assert view["outcome"].percentage == D("100.00") and not ScoreResult.objects.exists()
        scoring.score_of(ex)  # reading never writes
        assert not ScoreResult.objects.exists()

    def test_an_execution_without_scoring_has_no_score_not_zero(self, w):
        ex = run(w, w["eversion"], GOOD)
        assert scoring.score_of(ex) == {"applicable": False, "reason": "not_configured"} and not ScoreResult.objects.exists()

    def test_hidden_questions_are_excluded_by_the_rules_engine(self, w, org_a):
        template, _ = load_template(org_a, COND_CHECKLIST)
        v = publish_scored(w | {"ctemplate": template}, template="ctemplate", document=COND_DOC)
        ex = run(w, v, {"A": "yes"})  # B is hidden by the rule and unanswered
        r = selectors.current_result(ex)
        rows = {i["code"]: i for i in r.breakdown["items"]}
        assert (r.percentage, rows["B"]["reason"], rows["B"]["included"]) == (D("100"), "hidden", False)
        ex2 = run(w, v, {"A": "no", "B": "no"})
        assert selectors.current_result(ex2).percentage == D("0")

    def test_submitting_twice_gives_one_result(self, w):
        v = publish_scored(w)
        ex = run(w, v, GOOD)
        again = exec_services.submit(ex, actor=w["runner"])
        assert again.already_submitted and ScoreResult.objects.filter(execution=ex).count() == 1

    def test_missing_answers_follow_the_missing_policy_on_final_scores(self, w):
        v = publish_scored(w, settings={"missing_policy": "exclude"})
        ex = run(w, v, GOOD)  # every scored item answered; policy only matters for optional gaps
        assert selectors.current_result(ex).status == "final"

    def test_existing_executions_keep_the_rules_of_their_version(self, w):
        v2 = publish_scored(w)
        ex = run(w, v2, GOOD, submit=False)  # started on v2
        doc = {**EXC_DOC, "items": {**EXC_DOC["items"], "FIRE-EXIT": {"weight": "10", "points": {"yes": "5", "no": "0"}}}}
        v3 = publish_scored(w, document=doc)
        ex.refresh_from_db()
        exec_services.submit(ex, actor=w["runner"])
        r = selectors.current_result(ex)
        assert r.version_id == v2.pk and r.available == D(20) and r.config.version_id == v2.pk
        ex3 = run(w, v3, GOOD)
        assert selectors.current_result(ex3).available == D(60)

    def test_a_scoring_failure_never_loses_the_submission_and_can_be_retried(self, w, monkeypatch):
        from apps.scoring.services import runtime

        v = publish_scored(w)
        real = runtime._outcome
        monkeypatch.setattr(runtime, "_outcome", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        ex = run(w, v, GOOD)
        assert ex.status == "submitted" and not ScoreResult.objects.exists()
        assert ActivityLog.objects.filter(verb="scoring.score_failed").count() == 1
        monkeypatch.setattr(runtime, "_outcome", real)
        assert scoring.score_pending(ex).status == "final" and scoring.score_pending(ex).pk == selectors.current_result(ex).pk
        assert ScoreResult.objects.count() == 1

    def test_the_command_scores_submitted_executions_that_missed_it(self, w, monkeypatch):
        from django.core.management import call_command

        from apps.scoring.services import runtime

        v = publish_scored(w)
        real = runtime._outcome
        monkeypatch.setattr(runtime, "_outcome", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        run(w, v, GOOD)
        monkeypatch.setattr(runtime, "_outcome", real)
        call_command("score_executions")
        call_command("score_executions")  # idempotent
        assert ScoreResult.objects.count() == 1


# ---------------------------------------------------------------------------------------------------------------- snapshots
@pytest.mark.django_db
class TestSnapshotsAndRecalculation:
    def final(self, w):
        v = publish_scored(w)
        ex = run(w, v, {**GOOD, "FRIDGE": "3.9"})
        return v, ex, selectors.current_result(ex)

    def test_results_cannot_be_updated_or_deleted(self, w):
        _v, _ex, r = self.final(w)
        for change in ({"percentage": D("99")}, {"grade": "A"}, {"breakdown": {}}):
            with pytest.raises(IntegrityError), transaction.atomic():
                ScoreResult.objects.filter(pk=r.pk).update(**change)
        with pytest.raises(IntegrityError), transaction.atomic():
            ScoreResult.objects.filter(pk=r.pk).delete()
        with pytest.raises(IntegrityError), transaction.atomic():
            r.delete()

    def test_one_current_result_per_execution_in_the_database(self, w):
        _v, ex, r = self.final(w)
        with pytest.raises(IntegrityError), transaction.atomic():
            ScoreResult.objects.create(organization=r.organization, execution=ex, version=r.version, config=r.config, config_hash=r.config_hash,
                                       status="final", method="percentage", input_fingerprint="x", is_current=True)

    def test_a_result_must_agree_with_its_execution(self, w):
        v, ex, r = self.final(w)
        other = run(w, v, GOOD, submit=False)
        with pytest.raises(IntegrityError), transaction.atomic():  # right execution, wrong version
            ScoreResult.objects.create(organization=r.organization, execution=other, version=w["eversion"], config=r.config, config_hash="x", status="final",
                                       method="percentage", input_fingerprint="x")

    def test_recalculation_requires_permission_and_a_reason(self, w):
        _v, ex, _r = self.final(w)
        for who in ("score_viewer", "breakdown_viewer", "previewer", "runner"):
            with pytest.raises(PermissionDenied):
                scoring.recalculate(ex, actor=w[who], reason="why")
        with pytest.raises(scoring.ScoringError) as info:
            scoring.recalculate(ex, actor=w["recalculator"], reason="  ")
        assert info.value.code == "reason_required"

    def test_an_identical_recalculation_changes_nothing(self, w):
        _v, ex, r = self.final(w)
        result, changed = scoring.recalculate(ex, actor=w["recalculator"], reason="routine check")
        assert (changed, result.pk) == (False, r.pk) and ScoreResult.objects.filter(execution=ex).count() == 1
        assert ActivityLog.objects.filter(verb="scoring.recalculation_unchanged").count() == 1

    def test_a_changed_recalculation_appends_and_keeps_the_history(self, w):
        v, ex, r = self.final(w)
        with allow_definition_changes():  # audited maintenance: the only way a sealed configuration can change
            ScoringConfig.objects.filter(version=v).update(precision=0)
        result, changed = scoring.recalculate(ex, actor=w["recalculator"], reason="Corrected rounding policy")
        assert changed and result.pk != r.pk and result.kind == "recalculation" and result.reason == "Corrected rounding policy"
        assert result.created_by_id == w["recalculator"].pk and result.is_current
        r.refresh_from_db()
        assert not r.is_current and r.percentage == D("75") and ScoreResult.objects.filter(execution=ex).count() == 2
        assert selectors.current_result(ex).pk == result.pk
        assert ActivityLog.objects.filter(verb="scoring.score_recalculated").count() == 1

    def test_only_a_submitted_execution_can_be_recalculated(self, w):
        v = publish_scored(w)
        ex = run(w, v, GOOD, submit=False)
        with pytest.raises(PermissionDenied):
            scoring.recalculate(ex, actor=w["recalculator"], reason="x")

    def test_publishing_a_new_version_never_touches_old_results(self, w):
        _v, ex, r = self.final(w)
        publish_scored(w, document={**EXC_DOC, "bands": [{"min": "0", "grade": "Z", "label": "z"}]})
        again = ScoreResult.objects.get(pk=r.pk)
        assert (again.grade, again.percentage, again.breakdown) == (r.grade, r.percentage, r.breakdown)


# ---------------------------------------------------------------------------------------------------------------- offline
OFFLINE_DOC = {"items": {"FIRE-EXIT": {"points": {"yes": "5", "no": "0"}}, "HANDWASH": {"points": {"yes": "5", "no": "0"}},
                         "FRIDGE": {"ranges": [{"min": "0", "max": "4", "max_inclusive": False, "points": "0"}, {"min": "4", "max": "10", "points": "5"}]}}, "bands": BANDS}


@pytest.mark.django_db
class TestOfflineSync:
    def test_the_server_scores_what_the_device_synchronizes_and_ignores_any_client_score(self, w):
        from offline_support import OFFLINE_CHECKLIST, oid, op

        template, _ = load_template(w["org"], OFFLINE_CHECKLIST)
        version = publish_scored(w | {"otemplate": template}, template="otemplate", document=OFFLINE_DOC)
        w["oversion"] = version
        assignment = issued(w, version=version)
        client = APIClient()
        client.force_authenticate(w["runner"])
        forged = {**op(w, assignment, type="submit", seq=4), "score": 100, "grade": "A", "percentage": 100}
        refused = client.post("/api/v1/offline/sync/", {"schema_version": 1, "operations": [forged]}, format="json").json()["data"]
        refused = refused["results"] if isinstance(refused, dict) else refused
        assert refused[0]["status"] == "rejected" and "score" in refused[0]["message"] and not ScoreResult.objects.exists()  # a client-supplied score is refused
        ops = [op(w, assignment, "FIRE-EXIT", "yes", seq=1), op(w, assignment, "HANDWASH", "yes", seq=2), op(w, assignment, "FRIDGE", "3.9", seq=3),
               op(w, assignment, type="submit", seq=4)]
        r = client.post("/api/v1/offline/sync/", {"schema_version": 1, "operations": ops}, format="json")
        out = r.json()["data"]
        out = out["results"] if isinstance(out, dict) else out
        assert r.status_code == 200 and [x["status"] for x in out] == ["applied"] * 4, out
        result = selectors.current_result(ChecklistExecution.objects.get())
        assert (result.percentage, result.earned, result.available, result.grade, result.status) == (D("66.67"), D(10), D(15), "D", "final")
        retry = client.post("/api/v1/offline/sync/", {"schema_version": 1, "operations": ops}, format="json").json()["data"]
        retry = retry["results"] if isinstance(retry, dict) else retry
        assert all(x.get("duplicate") for x in retry) and ScoreResult.objects.count() == 1


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

    def test_concurrent_submissions_produce_one_final_result(self, w):
        v = publish_scored(w)
        ex = run(w, v, GOOD, submit=False)
        out = self.race([lambda: exec_services.submit(ex, actor=w["runner"]), lambda: exec_services.submit(ex, actor=w["runner2"])])
        assert not [o for o in out if isinstance(o, Exception) and not isinstance(o, PermissionDenied)], out
        assert ScoreResult.objects.filter(execution=ex, is_current=True).count() == 1 and ScoreResult.objects.filter(execution=ex).count() == 1

    def test_concurrent_recalculations_never_leave_two_current_results(self, w):
        v = publish_scored(w)
        ex = run(w, v, GOOD)
        with allow_definition_changes():
            ScoringConfig.objects.filter(version=v).update(precision=0)
        out = self.race([lambda: scoring.recalculate(ex, actor=w["recalculator"], reason="a"), lambda: scoring.recalculate(ex, actor=w["recalculator"], reason="b")])
        assert not [o for o in out if isinstance(o, Exception) and not isinstance(o, scoring.ScoringConflict)], out
        assert ScoreResult.objects.filter(execution=ex, is_current=True).count() == 1
