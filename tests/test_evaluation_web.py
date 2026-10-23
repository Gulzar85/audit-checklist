"""The evaluation screens and admin (Phase 16): editor, condition builder, preview, result card and page, authorization, CSRF, read-only admin."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.checklists import lifecycle
from apps.evaluation import selectors, services
from apps.evaluation.models import EvaluationActionRequest, EvaluationRuleSet, EvaluationRun
from apps.findings.models import Finding

from evaluation_support import FIRE_NO, GOOD, RULES, build_evaluation_world, publish_evaluated, run


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_evaluation_world(org_a, org_b, make_user, assign, make_type)


def login(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def draft(w):
    return lifecycle.create_draft(w["etemplate"], actor=w["admin"], change_summary="s")


def editor(version):
    return reverse("evaluation:editor", args=[version.pk])


def rule_url(version, code=None):
    return reverse("evaluation:rule", args=[version.pk, code]) if code else reverse("evaluation:rule_new", args=[version.pk])


NEW_RULE = {"do": "save", "code": "BAND", "name": "Band rule", "priority": "50", "enabled": "on", "outcome": "warning", "on_unknown": "skip", "message": "Look at it", "rows": "3",
            "root_logic": "AND", "src_0": "score", "fld_0": "percentage", "op_0": "LESS_THAN", "val_0": "70", "grp_0": "0", "src_1": "answer", "fld_1": "FIRE-EXIT", "op_1": "EQUALS",
            "val_1": "no", "grp_1": "1", "src_2": "", "grp_2": "0", "gl_1": "OR", "act_type_0": "tag", "act_tag_0": "band"}


@pytest.mark.django_db
class TestEditor:
    def test_an_empty_editor_offers_the_first_rule(self, w, draft):
        r = login(w["rule_designer"]).get(editor(draft))
        html = r.content.decode()
        assert r.status_code == 200 and "No rules yet" in html and "Add the first rule" in html and "Add at least one rule to try the evaluation" in html

    def test_a_configured_editor_shows_readable_rules_and_validation(self, w, draft):
        services.save_config(draft, actor=w["admin"], document=RULES)
        html = login(w["rule_designer"]).get(editor(draft)).content.decode()
        assert "Critical safety failure" in html and "score percentage is less than 80" in html and "Requests: Request a finding" in html
        assert "The rules are complete and valid" in html and 'hx-post=' in html and "Stops processing" not in html

    def test_problems_are_shown_next_to_the_rule(self, w, draft):
        services.save_config(draft, actor=w["admin"], document={"rules": [{"code": "X", "name": "x", "priority": 1, "outcome": "fail",
                                                                          "condition": {"source": "answer", "field": "NOPE", "operator": "EQUALS", "value": "no"}}]})
        html = login(w["rule_designer"]).get(editor(draft)).content.decode()
        assert "not a question of this checklist version" in html and "cannot be published yet" in html and "Needs attention" in html

    def test_published_versions_are_read_only(self, w):
        v = publish_evaluated(w)
        html = login(w["rule_designer"]).get(editor(v)).content.decode()
        assert "read-only" in html and "Save settings" not in html and "Add rule" not in html and "Move up" not in html

    def test_authorization_and_isolation(self, w, draft, make_user, org_b):
        assert Client().get(editor(draft)).status_code == 302
        for who in ("score_viewer", "result_viewer", "designer"):
            assert login(w[who]).get(editor(draft)).status_code in (403, 404)
        assert login(make_user(email="ev-web-out@example.com", organization=org_b)).get(editor(draft)).status_code == 404
        assert login(w["rule_designer"]).get(reverse("evaluation:editor", args=["00000000-0000-0000-0000-000000000000"])).status_code == 404


@pytest.mark.django_db
class TestRuleActions:
    def post(self, user, name, version, data=None, **kw):
        return login(user).post(reverse(f"evaluation:{name}", args=[version.pk, *kw.get("args", [])]), data or {}, follow=True)

    def test_settings_save(self, w, draft):
        r = self.post(w["rule_designer"], "settings", draft, {"notes": "n"})
        row = EvaluationRuleSet.objects.get(version=draft)
        assert row.enabled is False and row.notes == "n" and "settings saved" in r.content.decode()  # an unchecked box switches it off

    def test_toggle_move_and_delete(self, w, draft):
        services.save_config(draft, actor=w["admin"], document=RULES)
        d = w["rule_designer"]
        self.post(d, "rule_toggle", draft, {"action": "disable"}, args=["LOW"])
        assert [r["enabled"] for r in EvaluationRuleSet.objects.get(version=draft).document["rules"]] == [True, False, True]
        self.post(d, "rule_move", draft, {"direction": "up"}, args=["LOW"])
        assert [r["code"] for r in EvaluationRuleSet.objects.get(version=draft).document["rules"]][:2] == ["LOW", "CRIT"]
        self.post(d, "rule_delete", draft, {}, args=["NOSCORE"])
        assert [r["code"] for r in EvaluationRuleSet.objects.get(version=draft).document["rules"]] == ["LOW", "CRIT"]

    def test_only_designers_may_post_and_csrf_is_enforced(self, w, draft):
        services.save_config(draft, actor=w["admin"], document=RULES)
        for who in ("score_viewer", "result_viewer", "designer"):
            assert login(w[who]).post(reverse("evaluation:rule_toggle", args=[draft.pk, "LOW"]), {"action": "disable"}).status_code in (403, 404)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(w["rule_designer"])
        assert strict.post(reverse("evaluation:rule_toggle", args=[draft.pk, "LOW"]), {"action": "disable"}).status_code == 403
        assert EvaluationRuleSet.objects.get(version=draft).document["rules"][1]["enabled"] is True

    def test_a_published_version_refuses_edits_with_a_message(self, w):
        v = publish_evaluated(w)
        r = self.post(w["rule_designer"], "rule_toggle", v, {"action": "disable"}, args=["LOW"])
        assert "published" in r.content.decode() and EvaluationRuleSet.objects.get(version=v).document["rules"][1]["enabled"] is True


@pytest.mark.django_db
class TestConditionBuilder:
    def test_the_new_rule_form_offers_only_controlled_choices(self, w, draft):
        html = login(w["rule_designer"]).get(rule_url(draft)).content.decode()
        assert "Add a condition" in html and 'name="src_0"' in html and 'name="src_2"' in html
        assert "Request a finding" in html and "Start a workflow" in html and "Record a tag" in html
        for forbidden in ('name="sql"', 'name="expression"', 'name="python"', "<textarea"):  # no free-form code or expression input exists
            assert forbidden not in html

    def test_changing_the_source_re_renders_with_compatible_operators_only(self, w, draft):
        c = login(w["rule_designer"])
        r = c.post(rule_url(draft), {"rows": "1", "src_0": "score", "fld_0": "grade", "op_0": "EQUALS"}, HTTP_HX_REQUEST="true")
        html = r.content.decode()
        assert r.status_code == 200 and '<form id="rule-form"' in html and "<html" not in html  # a fragment
        assert 'value="CONTAINS"' in html and 'value="LESS_THAN"' not in html  # a grade is text: text operators, no ordering
        r = c.post(rule_url(draft), {"rows": "1", "src_0": "score", "fld_0": "percentage", "op_0": "CONTAINS"}, HTTP_HX_REQUEST="true")
        assert 'value="LESS_THAN"' in r.content.decode() and 'value="CONTAINS"' not in r.content.decode()  # an incompatible operator is repaired

    def test_the_value_widget_follows_the_field_and_operator(self, w, draft):
        c = login(w["rule_designer"])
        html = c.post(rule_url(draft), {"rows": "1", "src_0": "answer", "fld_0": "FRIDGE", "op_0": "BETWEEN"}, HTTP_HX_REQUEST="true").content.decode()
        assert 'name="val_0"' in html and 'name="val2_0"' in html  # a range asks for two numbers
        html = c.post(rule_url(draft), {"rows": "1", "src_0": "answer", "fld_0": "TAGS", "op_0": "CONTAINS_ANY"}, HTTP_HX_REQUEST="true").content.decode()
        assert 'name="vals_0"' in html and "Pests" in html
        html = c.post(rule_url(draft), {"rows": "1", "src_0": "answer", "fld_0": "FIRE-EXIT", "op_0": "IS_EMPTY"}, HTTP_HX_REQUEST="true").content.decode()
        assert 'name="val_0"' not in html  # no value for "is empty"

    def test_add_a_row_and_action_fields(self, w, draft):
        c = login(w["rule_designer"])
        html = c.post(rule_url(draft), {"do": "add_row", "rows": "2", "act_type_0": "request_finding"}, HTTP_HX_REQUEST="true").content.decode()
        assert 'name="rows" value="3"' in html and 'name="act_cat_0"' in html and "Quality" in html and "Critical" in html  # the organization's own categories and severities

    def test_saving_a_rule_with_groups_actions_and_a_negation(self, w, draft):
        r = login(w["rule_designer"]).post(rule_url(draft), NEW_RULE, follow=True)
        doc = EvaluationRuleSet.objects.get(version=draft).document["rules"][0]
        assert r.status_code == 200 and "Rule BAND saved" in r.content.decode()
        assert doc["condition"] == {"logic": "AND", "conditions": [{"source": "score", "field": "percentage", "operator": "LESS_THAN", "value": "70"},
                                                                    {"logic": "OR", "conditions": [{"source": "answer", "field": "FIRE-EXIT", "operator": "EQUALS", "value": "no"}]}]}
        assert doc["actions"] == [{"type": "tag", "tag": "band"}] and doc["priority"] == 50 and doc["stop"] is False

    def test_a_negated_row_becomes_a_not_group(self, w, draft):
        login(w["rule_designer"]).post(rule_url(draft), {**NEW_RULE, "neg_0": "on", "src_1": "", "grp_1": "0"})
        cond = EvaluationRuleSet.objects.get(version=draft).document["rules"][0]["condition"]
        assert cond["conditions"][0] == {"logic": "NOT", "conditions": [{"source": "score", "field": "percentage", "operator": "LESS_THAN", "value": "70"}]}

    def test_a_rule_with_problems_is_kept_as_a_draft_with_a_warning(self, w, draft):
        r = login(w["rule_designer"]).post(rule_url(draft), {**NEW_RULE, "val_0": "abc"}, follow=True)
        assert "cannot be published yet" in r.content.decode() and EvaluationRuleSet.objects.get(version=draft).document["rules"][0]["code"] == "BAND"
        assert any(i.code == "invalid_value" for i in services.validate_config(draft)[0])

    def test_editing_an_existing_rule_shows_its_rows(self, w, draft):
        services.save_config(draft, actor=w["admin"], document=RULES)
        html = login(w["rule_designer"]).get(rule_url(draft, "CRIT")).content.decode()
        assert 'value="critical"' in html and "FIRE-EXIT" in html and 'id="r-code"' in html and "Critical safety failure" in html
        assert "Finding title" in html and "Critical safety failure" in html and 'name="gl_1"' in html

    def test_a_rule_code_is_not_changed_when_editing(self, w, draft):
        services.save_config(draft, actor=w["admin"], document=RULES)
        c = login(w["rule_designer"])
        c.post(rule_url(draft, "LOW"), {**NEW_RULE, "code": "HACK", "priority": "20"}, follow=True)
        codes = [r["code"] for r in EvaluationRuleSet.objects.get(version=draft).document["rules"]]
        assert "HACK" not in codes and codes.count("LOW") == 1

    def test_a_condition_the_builder_cannot_show_is_kept_unchanged(self, w, draft):
        deep = {"logic": "AND", "conditions": [{"logic": "OR", "conditions": [{"logic": "AND", "conditions": [{"source": "score", "field": "critical", "operator": "EQUALS", "value": True}]}]}]}
        services.save_config(draft, actor=w["admin"], document={"rules": [{"code": "D", "name": "deep", "priority": 1, "outcome": "fail", "condition": deep}]})
        c = login(w["rule_designer"])
        html = c.get(rule_url(draft, "D")).content.decode()
        assert "keeps its current structure" in html and 'name="keep_condition"' in html
        c.post(rule_url(draft, "D"), {"do": "save", "name": "deep renamed", "priority": "1", "outcome": "fail", "on_unknown": "skip", "keep_condition": "1", "enabled": "on", "rows": "1"})
        saved = EvaluationRuleSet.objects.get(version=draft).document["rules"][0]
        assert saved["name"] == "deep renamed" and saved["condition"] == deep

    def test_designers_only(self, w, draft):
        for who in ("score_viewer", "result_viewer"):
            assert login(w[who]).get(rule_url(draft)).status_code in (403, 404)
            assert login(w[who]).post(rule_url(draft), NEW_RULE).status_code in (403, 404)


@pytest.mark.django_db
class TestPreviewScreen:
    def test_the_what_if_evaluates_with_the_same_engine_and_stores_nothing(self, w, draft):
        services.save_config(draft, actor=w["admin"], document=RULES)
        r = login(w["rule_designer"]).post(reverse("evaluation:preview", args=[draft.pk]), {"score_status": "final", "score_percentage": "62", "a_FIRE-EXIT": "yes"})
        html = r.content.decode()
        assert r.status_code == 200 and "Review required" in html and "observed: 62" in html and "not a recorded evaluation" in html
        assert "A real evaluation would request" in html and not Finding.objects.exists() and not EvaluationRun.objects.exists()

    def test_an_unavailable_score_leaves_score_rules_undecided(self, w, draft):
        services.save_config(draft, actor=w["admin"], document=RULES)
        html = login(w["rule_designer"]).post(reverse("evaluation:preview", args=[draft.pk]), {"score_status": "provisional", "score_percentage": "10", "a_FIRE-EXIT": "yes"}).content.decode()
        assert "could not be decided" in html and "unavailable" in html

    def test_an_invalid_rule_set_says_so(self, w, draft):
        services.save_config(draft, actor=w["admin"], document={"rules": [{"code": "X", "name": "x", "priority": 1, "outcome": "fail", "condition": {"source": "answer", "field": "NOPE", "operator": "EQUALS", "value": "no"}}]})
        html = login(w["rule_designer"]).post(reverse("evaluation:preview", args=[draft.pk]), {}).content.decode()
        assert "cannot be evaluated yet" in html

    def test_designers_only(self, w, draft):
        assert login(w["result_viewer"]).post(reverse("evaluation:preview", args=[draft.pk]), {}).status_code in (403, 404)


@pytest.mark.django_db
class TestExecutionScreens:
    @pytest.fixture
    def evaluated(self, w):
        v = publish_evaluated(w)
        return v, run(w, v, {**GOOD, **FIRE_NO})

    def test_the_execution_page_shows_a_card_to_those_who_may_see_the_result(self, w, evaluated):
        _v, ex = evaluated
        url = reverse("executions:detail", args=[ex.pk])
        html = login(w["result_viewer"]).get(url).content.decode()
        assert 'id="eval-title"' in html and "Fail" in html and "Critical safety failure" in html and "Details" in html
        assert "Why" in login(w["explainer"]).get(url).content.decode()
        assert 'id="eval-title"' not in login(w["no_eval"]).get(url).content.decode()  # seeing the execution is not seeing its evaluation

    def test_an_unevaluated_execution_has_no_card_and_no_invented_pass(self, w):
        ex = run(w, w["eversion"], GOOD)
        assert 'id="eval-title"' not in login(w["result_viewer"]).get(reverse("executions:detail", args=[ex.pk])).content.decode()
        assert "not evaluated automatically" in login(w["result_viewer"]).get(reverse("evaluation:execution", args=[ex.pk])).content.decode()

    def test_the_evaluation_page_explains_why_with_links_to_what_it_requested(self, w, evaluated):
        _v, ex = evaluated
        html = login(w["admin"]).get(reverse("evaluation:execution", args=[ex.pk])).content.decode()
        assert "Matched rules" in html and "matched because" in html and "(observed: no)" in html and "Requested actions" in html
        finding = Finding.objects.first()
        assert reverse("findings:detail", args=[finding.pk]) in html and "Succeeded" in html and "highest-precedence" in html

    def test_the_summary_viewer_does_not_get_the_explanation(self, w, evaluated):
        _v, ex = evaluated
        html = login(w["result_viewer"]).get(reverse("evaluation:execution", args=[ex.pk])).content.decode()
        assert "Fail" in html and "matched because" not in html and "needs the" in html and "Requested actions" not in html

    def test_authorization_and_isolation(self, w, evaluated, make_user, org_b):
        _v, ex = evaluated
        url = reverse("evaluation:execution", args=[ex.pk])
        assert Client().get(url).status_code == 302
        assert login(w["no_eval"]).get(url).status_code == 403
        assert login(make_user(email="ev-web-x@example.com", organization=org_b)).get(url).status_code == 404

    def test_reevaluation_needs_permission_and_a_reason(self, w, evaluated):
        _v, ex = evaluated
        url = reverse("evaluation:reevaluate", args=[ex.pk])
        assert login(w["explainer"]).post(url, {"reason": "x"}).status_code == 403
        assert "reason is required" in login(w["reevaluator"]).post(url, {"reason": ""}, follow=True).content.decode()
        r = login(w["reevaluator"]).post(url, {"reason": "audit"}, follow=True)
        assert "nothing changed" in r.content.decode() and EvaluationRun.objects.filter(execution=ex).count() == 1 and Finding.objects.count() == 2

    def test_an_execution_in_progress_shows_a_provisional_card_only_to_previewers(self, w):
        v = publish_evaluated(w)
        ex = run(w, v, FIRE_NO, submit=False)
        url = reverse("executions:detail", args=[ex.pk])
        assert "Provisional" in login(w["evaluator"]).get(url).content.decode()
        assert "Provisional" not in login(w["result_viewer"]).get(url).content.decode()
        assert not selectors.runs_for(ex) and not EvaluationActionRequest.objects.exists()


@pytest.mark.django_db
class TestBuilderAndAdmin:
    def test_the_builder_links_to_evaluation_for_those_who_may_read_it(self, w, draft):
        assert reverse("evaluation:editor", args=[draft.pk]) in login(w["admin"]).get(reverse("checklists:builder", args=[w["etemplate"].pk])).content.decode()

    def test_the_version_page_links_to_configured_rules(self, w):
        v = publish_evaluated(w)
        html = login(w["admin"]).get(reverse("checklists:version_detail", args=[w["etemplate"].pk, v.version_number])).content.decode()
        assert reverse("evaluation:editor", args=[v.pk]) in html

    def test_the_admin_is_read_only(self, w, django_user_model):
        v = publish_evaluated(w)
        ex = run(w, v, {**GOOD, **FIRE_NO})
        su = django_user_model.objects.create_superuser(email="su-ev@example.com", password="x-Strong-pass-123")
        client = login(su)
        for model in ("evaluationruleset", "evaluationrun", "evaluationactionrequest"):
            assert client.get(reverse(f"admin:evaluation_{model}_changelist")).status_code == 200
            assert client.get(reverse(f"admin:evaluation_{model}_add")).status_code == 403
        result = selectors.current_run(ex)
        assert "_save" not in client.get(reverse("admin:evaluation_evaluationrun_change", args=[result.pk])).content.decode()
        assert client.post(reverse("admin:evaluation_evaluationrun_delete", args=[result.pk]), {"post": "yes"}).status_code == 403
        assert EvaluationRun.objects.filter(pk=result.pk).exists()
