"""The scoring screens and admin (Phase 15): editor, item rules, preview, score card and page, authorization, CSRF, read-only admin."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.checklists import lifecycle
from apps.scoring import selectors, services
from apps.scoring.models import ScoreResult, ScoringConfig

from scoring_support import EXC_DOC, GOOD, build_scoring_world, publish_scored, run


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_scoring_world(org_a, org_b, make_user, assign, make_type)


def login(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def draft(w):
    return lifecycle.create_draft(w["etemplate"], actor=w["admin"], change_summary="s")


def editor(version):
    return reverse("scoring:editor", args=[version.pk])


@pytest.mark.django_db
class TestEditor:
    def test_a_designer_sees_an_empty_editor_with_every_card(self, w, draft):
        r = login(w["designer"]).get(editor(draft))
        html = r.content.decode()
        assert r.status_code == 200
        for text in ("Settings", "Questions", "Section weights", "Grade bands", "Critical failures", "Try it", "Cannot be scored"):
            assert text in html
        assert "Save settings" in html and 'hx-post="' not in html.split("Try it")[1][:2000]  # nothing to preview yet

    def test_a_configured_editor_shows_summaries_and_validation(self, w, draft):
        services.save_config(draft, actor=w["admin"], document=EXC_DOC)
        html = login(w["designer"]).get(editor(draft)).content.decode()
        assert "Yes → 5 · No → 0 · weight 2" in html and "Fire exit blocked" in html and "The scoring is complete and valid" in html
        assert 'hx-post=' in html

    def test_problems_are_shown_next_to_the_question(self, w, draft):
        services.save_config(draft, actor=w["admin"], document={"items": {"FIRE-EXIT": {"points": {"yes": "5"}}}})
        html = login(w["designer"]).get(editor(draft)).content.decode()
        assert "give points for no" in html and "cannot be published yet" in html

    def test_published_versions_are_read_only(self, w):
        v = publish_scored(w)
        html = login(w["designer"]).get(editor(v)).content.decode()
        assert "read-only" in html and "Save settings" not in html and "disabled" in html

    def test_authorization_and_isolation(self, w, draft, make_user, org_b):
        assert Client().get(editor(draft)).status_code == 302  # to the sign-in page
        assert login(w["score_viewer"]).get(editor(draft)).status_code in (403, 404)
        assert login(make_user(email="sc-web-out@example.com", organization=org_b)).get(editor(draft)).status_code == 404
        assert login(w["designer"]).get(reverse("scoring:editor", args=["00000000-0000-0000-0000-000000000000"])).status_code == 404


@pytest.mark.django_db
class TestEditorActions:
    def post(self, user, name, version, data, **kw):
        return login(user).post(reverse(f"scoring:{name}", args=[version.pk, *kw.get("args", [])]), data, follow=True)

    def test_settings_save_and_flash(self, w, draft):
        r = self.post(w["designer"], "settings", draft, {"enabled": "on", "method": "weighted_sections", "na_policy": "zero", "missing_policy": "exclude", "precision": "1",
                                                        "critical_label": "FAIL", "critical_overrides_grade": "on", "notes": "n"})
        row = ScoringConfig.objects.get(version=draft)
        assert (row.method, row.na_policy, row.missing_policy, row.precision, row.critical_overrides_grade, row.notes) == ("weighted_sections", "zero", "exclude", 1, True, "n")
        assert "Scoring settings saved" in r.content.decode()

    def test_unchecking_the_box_switches_scoring_off(self, w, draft):
        self.post(w["designer"], "settings", draft, {"method": "percentage", "na_policy": "exclude", "missing_policy": "zero", "precision": "2"})
        assert ScoringConfig.objects.get(version=draft).enabled is False

    def test_bad_settings_are_flashed_not_a_500(self, w, draft):
        r = self.post(w["designer"], "settings", draft, {"enabled": "on", "method": "percentage", "na_policy": "exclude", "missing_policy": "zero", "precision": "abc"})
        assert r.status_code == 200 and "whole number from 0 to 4" in r.content.decode() and not ScoringConfig.objects.exists()

    def test_bands_critical_and_weights(self, w, draft):
        services.save_config(draft, actor=w["admin"], document=EXC_DOC)
        d = w["designer"]
        self.post(d, "bands", draft, {"band_min": ["0", "50", ""], "band_grade": ["F", "P", ""], "band_label": ["Fail", "Pass", ""]})
        self.post(d, "critical", draft, {"crit_item": ["HANDWASH", ""], "crit_op": ["equals", ""], "crit_value": ["no", ""], "crit_label": ["Dirty", ""]})
        self.post(d, "weights", draft, {"w_FIRE": "3", "w_FOOD": ""})
        doc = ScoringConfig.objects.get(version=draft).document
        assert [b["grade"] for b in doc["bands"]] == ["F", "P"] and doc["critical"][0]["item"] == "HANDWASH" and doc["sections"] == {"FIRE": "3"}
        assert len(doc["items"]) == 3  # the other parts are untouched

    def test_invalid_bands_are_flashed(self, w, draft):
        r = self.post(w["designer"], "bands", draft, {"band_min": ["x"], "band_grade": ["A"], "band_label": [""]})
        assert "not a number" in r.content.decode() or "number" in r.content.decode()

    def test_only_designers_may_post_and_csrf_is_enforced(self, w, draft):
        for who in ("score_viewer", "breakdown_viewer", "exec_viewer"):
            assert login(w[who]).post(reverse("scoring:settings", args=[draft.pk]), {"enabled": "on"}).status_code in (403, 404)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(w["designer"])
        assert strict.post(reverse("scoring:settings", args=[draft.pk]), {"enabled": "on"}).status_code == 403
        assert not ScoringConfig.objects.exists()

    def test_a_published_version_refuses_edits_with_a_message(self, w):
        v = publish_scored(w)
        r = self.post(w["designer"], "settings", v, {"enabled": "on", "method": "percentage", "na_policy": "exclude", "missing_policy": "zero", "precision": "0"})
        assert "published" in r.content.decode() and ScoringConfig.objects.get(version=v).precision == 2


@pytest.mark.django_db
class TestItemRuleScreens:
    def item(self, draft, code):
        return reverse("scoring:item", args=[draft.pk, code])

    @pytest.mark.parametrize("code", ["FIRE-EXIT", "HANDWASH", "BOOL", "FRIDGE", "KIND", "TAGS", "RATING"])
    def test_every_scorable_type_has_a_form(self, w, draft, code):
        assert login(w["designer"]).get(self.item(draft, code)).status_code == 200

    def test_unscorable_types_explain_and_unknown_codes_are_404(self, w, draft):
        c = login(w["designer"])
        assert "never scored" in c.get(self.item(draft, "NOTE")).content.decode()
        assert c.get(self.item(draft, "NOPE")).status_code == 404

    def test_saving_a_yes_no_rule_then_removing_it(self, w, draft):
        c = login(w["designer"])
        c.post(self.item(draft, "FIRE-EXIT"), {"weight": "2", "p_yes": "5", "p_no": "0"})
        assert ScoringConfig.objects.get(version=draft).document["items"]["FIRE-EXIT"]["points"] == {"no": "0", "yes": "5"}
        assert "5" in c.get(self.item(draft, "FIRE-EXIT")).content.decode()
        c.post(self.item(draft, "FIRE-EXIT"), {"remove": "1"})
        assert "FIRE-EXIT" not in ScoringConfig.objects.get(version=draft).document["items"]

    def test_saving_choice_and_numeric_rules(self, w, draft):
        c = login(w["designer"])
        c.post(self.item(draft, "TAGS"), {"weight": "1", "aggregate": "max", "p_pest": "0", "p_leak": "1", "p_dirt": "2"})
        c.post(self.item(draft, "FRIDGE"), {"weight": "1", "r0_min": "0", "r0_max": "4", "r0_points": "0", "r0_mininc": "on",
                                            "r1_min": "4", "r1_max": "10", "r1_points": "5", "r1_mininc": "on", "r1_maxinc": "on"})
        items = ScoringConfig.objects.get(version=draft).document["items"]
        assert items["TAGS"]["aggregate"] == "max" and items["TAGS"]["points"]["dirt"] == "2"
        assert [r["points"] for r in items["FRIDGE"]["ranges"]] == ["0", "5"] and items["FRIDGE"]["ranges"][0]["max_inclusive"] is False

    def test_a_bad_value_is_flashed_and_nothing_is_saved(self, w, draft):
        r = login(w["designer"]).post(self.item(draft, "FIRE-EXIT"), {"weight": "1", "p_yes": "abc", "p_no": "0"}, follow=True)
        assert "not a number" in r.content.decode() and not ScoringConfig.objects.exists()

    def test_viewers_cannot_post(self, w, draft):
        assert login(w["score_viewer"]).post(self.item(draft, "FIRE-EXIT"), {"weight": "1", "p_yes": "1", "p_no": "0"}).status_code in (403, 404)


@pytest.mark.django_db
class TestPreviewScreen:
    def test_the_what_if_scores_with_the_same_engine_and_stores_nothing(self, w, draft):
        services.save_config(draft, actor=w["admin"], document=EXC_DOC)
        r = login(w["designer"]).post(reverse("scoring:preview", args=[draft.pk]), {"a_FIRE-EXIT": "yes", "a_HANDWASH": "no", "a_FRIDGE": "4.5", "final": "on"})
        html = r.content.decode()
        assert r.status_code == 200 and "75%" in html and "Earned 10 of 5" not in html and "Satisfactory" in html and "grade" not in html.lower().split("result")[0]
        assert not ScoreResult.objects.exists()

    def test_critical_and_not_applicable_are_visible(self, w, draft):
        services.save_config(draft, actor=w["admin"], document=EXC_DOC)
        html = login(w["designer"]).post(reverse("scoring:preview", args=[draft.pk]), {"a_FIRE-EXIT": "no", "na_HANDWASH": "on", "final": "on"}).content.decode()
        assert "Critical failure" in html and "Fire exit blocked" in html
        assert "Excluded: the answer is invalid" in html  # a required question cannot be N/A (Phase 6): excluded and flagged, never scored as zero

    def test_an_invalid_configuration_says_so(self, w, draft):
        services.save_config(draft, actor=w["admin"], document={"items": {"FIRE-EXIT": {"points": {"yes": "5"}}}})
        html = login(w["designer"]).post(reverse("scoring:preview", args=[draft.pk]), {"a_FIRE-EXIT": "yes"}).content.decode()
        assert "cannot be scored yet" in html and "give points for no" in html

    def test_designers_only(self, w, draft):
        assert login(w["score_viewer"]).post(reverse("scoring:preview", args=[draft.pk]), {}).status_code in (403, 404)


@pytest.mark.django_db
class TestExecutionScoreScreens:
    @pytest.fixture
    def scored(self, w):
        v = publish_scored(w)
        return v, run(w, v, {**GOOD, "FRIDGE": "3.9"})

    def test_the_execution_page_shows_a_score_card_to_those_who_may_see_the_score(self, w, scored):
        _v, ex = scored
        url = reverse("executions:detail", args=[ex.pk])
        html = login(w["score_viewer"]).get(url).content.decode()
        assert "Score" in html and "75%" in html and "Final" in html and "How it was calculated" not in html
        assert "How it was calculated" in login(w["breakdown_viewer"]).get(url).content.decode()
        assert 'id="score-title"' not in login(w["exec_viewer"]).get(url).content.decode()  # seeing the execution is not seeing its score

    def test_an_unscored_execution_has_no_card_and_no_invented_zero(self, w):
        ex = run(w, w["eversion"], GOOD)
        html = login(w["score_viewer"]).get(reverse("executions:detail", args=[ex.pk])).content.decode()
        assert 'id="score-title"' not in html
        assert "not scored" in login(w["score_viewer"]).get(reverse("scoring:execution", args=[ex.pk])).content.decode()

    def test_the_breakdown_page(self, w, scored):
        _v, ex = scored
        r = login(w["breakdown_viewer"]).get(reverse("scoring:execution", args=[ex.pk]))
        html = r.content.decode()
        assert r.status_code == 200 and "How this score was calculated" in html and "FIRE-EXIT" in html and "weight 2" in html and "Treatment" in html
        assert "Recalculate" not in html  # needs its own permission
        assert "Recalculate" in login(w["recalculator"]).get(reverse("scoring:execution", args=[ex.pk])).content.decode()

    def test_the_summary_viewer_does_not_get_the_breakdown(self, w, scored):
        _v, ex = scored
        html = login(w["score_viewer"]).get(reverse("scoring:execution", args=[ex.pk])).content.decode()
        assert "75%" in html and "How this score was calculated" not in html and "needs the" in html

    def test_authorization_and_isolation(self, w, scored, make_user, org_b):
        _v, ex = scored
        url = reverse("scoring:execution", args=[ex.pk])
        assert Client().get(url).status_code == 302
        assert login(w["exec_viewer"]).get(url).status_code == 403
        assert login(make_user(email="sc-web-x@example.com", organization=org_b)).get(url).status_code == 404

    def test_recalculation_needs_permission_and_a_reason(self, w, scored):
        _v, ex = scored
        url = reverse("scoring:recalculate", args=[ex.pk])
        assert login(w["breakdown_viewer"]).post(url, {"reason": "x"}).status_code == 403
        r = login(w["recalculator"]).post(url, {"reason": ""}, follow=True)
        assert "reason is required" in r.content.decode()
        r = login(w["recalculator"]).post(url, {"reason": "audit"}, follow=True)
        assert "nothing changed" in r.content.decode() and ScoreResult.objects.filter(execution=ex).count() == 1

    def test_an_execution_in_progress_shows_a_provisional_card_only_to_previewers(self, w):
        v = publish_scored(w)
        ex = run(w, v, {"FIRE-EXIT": "yes"}, submit=False)
        url = reverse("executions:detail", args=[ex.pk])
        assert "Provisional" in login(w["previewer"]).get(url).content.decode()
        assert "Provisional" not in login(w["score_viewer"]).get(url).content.decode()
        assert not selectors.results_for(ex)


@pytest.mark.django_db
class TestBuilderAndAdmin:
    def test_the_builder_links_to_scoring_for_those_who_may_read_it(self, w, draft):
        assert reverse("scoring:editor", args=[draft.pk]) in login(w["admin"]).get(reverse("checklists:builder", args=[w["etemplate"].pk])).content.decode()

    def test_the_version_page_links_to_configured_scoring(self, w):
        v = publish_scored(w)
        html = login(w["admin"]).get(reverse("checklists:version_detail", args=[w["etemplate"].pk, v.version_number])).content.decode()
        assert reverse("scoring:editor", args=[v.pk]) in html

    def test_the_admin_is_read_only(self, w, django_user_model):
        v = publish_scored(w)
        ex = run(w, v, GOOD)
        su = django_user_model.objects.create_superuser(email="su-sc@example.com", password="x-Strong-pass-123")
        client = login(su)
        for model in ("scoringconfig", "scoreresult"):
            assert client.get(reverse(f"admin:scoring_{model}_changelist")).status_code == 200
            assert client.get(reverse(f"admin:scoring_{model}_add")).status_code == 403
        result = selectors.current_result(ex)
        page = client.get(reverse("admin:scoring_scoreresult_change", args=[result.pk])).content.decode()
        assert "_save" not in page and "delete" not in page.lower().split("</form>")[0][-400:]
        assert client.post(reverse("admin:scoring_scoreresult_delete", args=[result.pk]), {"post": "yes"}).status_code == 403
        assert ScoreResult.objects.filter(pk=result.pk).exists()
