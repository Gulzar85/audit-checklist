"""The evaluation REST API (Phase 16): configuration, validation, preview, results, explanations, re-evaluation - and its security."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.evaluation import selectors
from apps.evaluation.models import EvaluationRun
from apps.findings.models import Finding
from apps.workflows.models import WorkflowInstance

from evaluation_support import FIRE_NO, GOOD, RULES, build_evaluation_world, publish_evaluated, rules_with, run

BASE = "/api/v1/"


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_evaluation_world(org_a, org_b, make_user, assign, make_type)


def api(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user)
    return client


def data(response):
    body = response.json()
    return body.get("data", body) if isinstance(body, dict) else body


@pytest.fixture
def evaluated(w):
    version = publish_evaluated(w)
    return version, run(w, version, {**GOOD, **FIRE_NO})


@pytest.mark.django_db
class TestMetadataAndRoot:
    def test_metadata_lists_the_finite_vocabularies(self, w):
        d = data(api(w["viewer"]).get(BASE + "evaluation/metadata/"))
        assert set(d["sources"]) == {"answer", "score", "execution"} and {f["code"] for f in d["fields"]["score"]} >= {"percentage", "grade", "critical", "status"}
        assert d["rule_outcomes"] == ["pass", "warning", "review_required", "fail"] and d["groups"] == ["AND", "OR", "NOT"] and d["limits"]["depth"] == 4
        assert {a["value"] for a in d["actions"]} == {"request_finding", "start_workflow", "tag"} and any(o["code"] == "LESS_THAN" for o in d["operators"])

    def test_metadata_needs_authentication(self):
        assert api().get(BASE + "evaluation/metadata/").status_code in (401, 403)

    def test_the_api_root_lists_it(self, w):
        assert "evaluation_metadata" in data(api(w["viewer"]).get(BASE))["endpoints"]


@pytest.mark.django_db
class TestConfigurationEndpoint:
    def draft(self, w):
        from apps.checklists import lifecycle

        return lifecycle.create_draft(w["etemplate"], actor=w["admin"], change_summary="s")

    def url(self, version, suffix=""):
        return f"{BASE}checklist-versions/{version.pk}/evaluation/{suffix}"

    def test_a_designer_saves_reads_validates_and_previews(self, w):
        draft = self.draft(w)
        c = api(w["rule_designer"])
        assert data(c.get(self.url(draft)))["configured"] is False
        r = c.put(self.url(draft), {"document": RULES, "notes": "v1"}, format="json")
        assert r.status_code == 200 and data(r)["editable"] and not [i for i in data(r)["issues"] if i["level"] == "error"] and [x["code"] for x in data(r)["document"]["rules"]] == ["CRIT", "LOW", "NOSCORE"]
        validated = data(c.post(self.url(draft, "validate/"), {}, format="json"))
        assert validated["ok"] is True and validated["rules"] == 3 and {i["code"] for i in validated["issues"]} == {"no_message"}  # warnings advise, they never block
        score = {"status": "final", "percentage": "62", "critical": False}
        d = data(c.post(self.url(draft, "preview/"), {"answers": {"FIRE-EXIT": "yes"}, "score": score}, format="json"))
        assert d["valid"] and d["non_production"] and d["result"]["outcome"] == "review_required" and d["result"]["matched"][0]["code"] == "LOW"
        assert "(observed: 62)" in d["result"]["explanation"][0] and d["would_request"][0]["type"] == "request_finding"

    def test_preview_has_no_side_effects_and_labels_itself_non_production(self, w):
        draft = self.draft(w)
        c = api(w["rule_designer"])
        c.put(self.url(draft), {"document": RULES}, format="json")
        d = data(c.post(self.url(draft, "preview/"), {"answers": {"FIRE-EXIT": "no"}, "score": {"status": "final", "percentage": "10", "critical": True}}, format="json"))
        assert d["result"]["outcome"] == "fail" and d["non_production"]
        assert not Finding.objects.exists() and not WorkflowInstance.objects.exists() and not EvaluationRun.objects.exists()

    def test_preview_reports_invalid_rules_and_refuses_unknown_inputs(self, w):
        draft = self.draft(w)
        c = api(w["rule_designer"])
        bad = {"rules": [{"code": "X", "name": "x", "priority": 1, "outcome": "fail", "condition": {"source": "answer", "field": "NOPE", "operator": "EQUALS", "value": "no"}}]}
        d = data(c.post(self.url(draft, "preview/"), {"document": bad}, format="json"))
        assert d["valid"] is False and d["result"] is None and d["issues"][0]["code"] == "unknown_item"
        assert c.post(self.url(draft, "preview/"), {"document": RULES, "answers": {"NOPE": "x"}}, format="json").status_code == 400
        assert c.post(self.url(draft, "preview/"), {"document": RULES, "score": {"status": "weird"}}, format="json").status_code == 400
        assert c.post(self.url(draft, "preview/"), {"outcome": "pass"}, format="json").status_code == 400  # fabricated results are refused, not ignored

    @pytest.mark.parametrize("body", [{"document": {"rules": "x"}}, {"document": {"eval": "1"}}, {"enabled": "maybe"}, {"outcome": "pass"}, {"actions": []}])
    def test_malformed_input_is_a_400(self, w, body):
        assert api(w["rule_designer"]).put(self.url(self.draft(w)), body, format="json").status_code == 400

    def test_arbitrary_code_in_a_rule_is_never_stored_as_executable_and_is_flagged(self, w):
        draft = self.draft(w)
        c = api(w["rule_designer"])
        evil = {"rules": [{"code": "E", "name": "e", "priority": 1, "outcome": "fail", "script": "__import__('os').system('x')",
                           "condition": {"source": "answer", "field": "FIRE-EXIT", "operator": "EQUALS", "value": "__import__('os')"}, "actions": [{"type": "shell", "cmd": "rm -rf /"}]}]}
        d = data(c.put(self.url(draft), {"document": evil}, format="json"))
        codes = {i["code"] for i in d["issues"] if i["level"] == "error"}
        assert {"malformed", "unknown_action", "invalid_value"} & codes and not Finding.objects.exists()

    def test_a_published_version_is_read_only_409(self, w):
        v = publish_evaluated(w)
        c = api(w["rule_designer"])
        assert c.put(self.url(v), {"enabled": False}, format="json").status_code == 409
        assert not data(c.get(self.url(v)))["editable"] and c.delete(self.url(v)).status_code == 409

    def test_only_rule_designers_may_write_and_viewing_a_checklist_is_not_enough(self, w):
        draft = self.draft(w)
        for who in ("viewer", "score_viewer", "result_viewer", "designer"):
            c = api(w[who])
            assert c.put(self.url(draft), {"document": RULES}, format="json").status_code in (403, 404)
            assert c.post(self.url(draft, "preview/"), {"document": RULES}, format="json").status_code in (403, 404)

    def test_reading_the_rules_needs_evaluation_view(self, w, org_a, make_user, assign):
        from apps.permissions import services as rbac
        from apps.permissions.registry import P

        draft = self.draft(w)
        reader = make_user(email="ev-reader@example.com", organization=org_a, role=None)
        assign(reader, org_a, rbac.create_role(organization=org_a, name="Checklist reader 2", permissions=[P.CHECKLIST_VIEW]))
        assert api(reader).get(f"{BASE}checklist-versions/{draft.pk}/").status_code == 200
        assert api(reader).get(self.url(draft)).status_code == 403
        assert api(w["rule_designer"]).get(self.url(draft)).status_code == 200

    def test_isolation_and_malformed_ids(self, w, make_user, org_b):
        draft = self.draft(w)
        c = api(make_user(email="ev-api-out@example.com", organization=org_b))
        assert c.get(self.url(draft)).status_code == 404 and c.put(self.url(draft), {}, format="json").status_code == 404 and c.post(self.url(draft, "preview/"), {}, format="json").status_code == 404
        assert api().get(self.url(draft)).status_code in (401, 403)
        assert api(w["rule_designer"]).get(f"{BASE}checklist-versions/not-a-uuid/evaluation/").status_code == 404


@pytest.mark.django_db
class TestExecutionEndpoints:
    def url(self, ex, suffix=""):
        return f"{BASE}executions/{ex.pk}/evaluation/{suffix}"

    def test_a_result_viewer_sees_the_outcome_but_not_the_observed_values(self, w, evaluated):
        _v, ex = evaluated
        c = api(w["result_viewer"])
        d = data(c.get(self.url(ex)))
        assert (d["stored"], d["outcome"], d["trigger"], d["is_current"]) == (True, "fail", "submission", True)
        assert [m["code"] for m in d["matched"]] == ["CRIT", "LOW"] and "explanation" not in d and "rules" not in d
        assert c.get(self.url(ex, "detail/")).status_code == 403

    def test_an_explainer_sees_rules_conditions_explanations_and_action_outcomes(self, w, evaluated):
        _v, ex = evaluated
        d = data(api(w["explainer"]).get(self.url(ex, "detail/")))
        crit = next(r for r in d["rules"] if r["code"] == "CRIT")
        assert crit["status"] == "matched" and "(observed: no)" in crit["explanation"] and crit["conditions"]["children"][0]["result"] in ("true", "false")
        assert any("highest-precedence" in line for line in d["explanation"]) and {a["type"] for a in d["actions"]} == {"request_finding", "start_workflow", "tag"}
        assert all(a["result"] is None for a in d["actions"] if a["type"] != "tag")  # the explainer cannot open findings or workflows

    def test_links_to_findings_and_workflows_appear_only_for_those_who_may_see_them(self, w, evaluated):
        _v, ex = evaluated
        d = data(api(w["admin"]).get(self.url(ex, "detail/")))
        refs = {a["result"]["type"]: a["result"] for a in d["actions"] if a["result"]}
        assert refs["finding"]["reference"].startswith("FND") or refs["finding"]["reference"] and refs["workflow"]["status"] == "active"

    def test_seeing_the_execution_is_not_seeing_its_evaluation(self, w, evaluated):
        _v, ex = evaluated
        assert api(w["no_eval"]).get(f"{BASE}executions/{ex.pk}/").status_code == 200
        for suffix in ("", "detail/", "runs/"):
            assert api(w["no_eval"]).get(self.url(ex, suffix)).status_code == 403

    def test_history_and_run_detail(self, w, evaluated):
        _v, ex = evaluated
        runs = data(api(w["result_viewer"]).get(self.url(ex, "runs/")))["results"]
        assert len(runs) == 1
        run_id = runs[0]["id"]
        assert data(api(w["explainer"]).get(f"{BASE}evaluation-runs/{run_id}/"))["actions"]
        assert "actions" not in data(api(w["result_viewer"]).get(f"{BASE}evaluation-runs/{run_id}/"))
        assert api(w["no_eval"]).get(f"{BASE}evaluation-runs/{run_id}/").status_code in (403, 404)

    def test_isolation(self, w, evaluated, make_user, org_b):
        _v, ex = evaluated
        run_id = selectors.current_run(ex).pk
        c = api(make_user(email="ev-x@example.com", organization=org_b))
        assert c.get(self.url(ex)).status_code == 404 and c.get(f"{BASE}evaluation-runs/{run_id}/").status_code == 404
        assert api().get(self.url(ex)).status_code in (401, 403)
        assert api(w["result_viewer"]).get(f"{BASE}executions/00000000-0000-0000-0000-000000000000/evaluation/").status_code == 404

    def test_an_unevaluated_version_says_not_applicable(self, w):
        ex = run(w, w["eversion"], GOOD)
        assert data(api(w["result_viewer"]).get(self.url(ex))) == {"applicable": False, "reason": "not_configured"}

    def test_an_execution_in_progress_is_provisional_and_only_for_evaluators(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, FIRE_NO, submit=False)
        assert api(w["result_viewer"]).get(self.url(ex)).status_code == 403
        d = data(api(w["evaluator"]).get(self.url(ex)))
        assert (d["stored"], d["provisional"], d["outcome"]) == (False, True, "fail") and not EvaluationRun.objects.exists() and not Finding.objects.exists()

    def test_nothing_writes_a_result_or_runs_an_action_directly(self, w, evaluated):
        _v, ex = evaluated
        c = api(w["reevaluator"])
        for method in (c.put, c.patch, c.delete):
            assert method(self.url(ex), {"outcome": "pass"}, format="json").status_code == 405
        for body in ({"reason": "x", "outcome": "pass"}, {"reason": "x", "actions": [{"type": "tag", "tag": "x"}]}, {"reason": "x", "evaluation": {}}):
            assert c.post(self.url(ex, "evaluate/"), body, format="json").status_code == 400
        assert selectors.current_run(ex).outcome == "fail" and EvaluationRun.objects.filter(execution=ex).count() == 1

    def test_reevaluation_needs_permission_and_a_reason(self, w, evaluated):
        _v, ex = evaluated
        assert api(w["explainer"]).post(self.url(ex, "evaluate/"), {"reason": "x"}, format="json").status_code == 403
        c = api(w["reevaluator"])
        assert c.post(self.url(ex, "evaluate/"), {}, format="json").status_code == 400
        d = data(c.post(self.url(ex, "evaluate/"), {"reason": "Quarterly check"}, format="json"))
        assert d["changed"] is False and d["outcome"] == "fail" and EvaluationRun.objects.filter(execution=ex).count() == 1
        assert Finding.objects.count() == 2 and WorkflowInstance.objects.count() == 1  # nothing was requested twice

    def test_a_published_rule_change_does_not_alter_existing_results_through_the_api(self, w, evaluated):
        v, ex = evaluated
        publish_evaluated(w, rules=rules_with("LOW"))
        assert data(api(w["result_viewer"]).get(self.url(ex)))["outcome"] == "fail" and str(selectors.current_run(ex).version_id) == str(v.pk)
