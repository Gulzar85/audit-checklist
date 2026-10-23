"""The scoring REST API (Phase 15): configuration, validation, preview, scores, breakdown, recalculation - and its security."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.scoring import selectors
from apps.scoring.models import ScoreResult

from scoring_support import EXC_DOC, GOOD, build_scoring_world, publish_scored, run

BASE = "/api/v1/"


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_scoring_world(org_a, org_b, make_user, assign, make_type)


def api(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user)
    return client


def data(response):
    body = response.json()
    return body.get("data", body) if isinstance(body, dict) else body


@pytest.fixture
def scored(w):
    version = publish_scored(w)
    return version, run(w, version, GOOD)


@pytest.mark.django_db
class TestMetadataAndRoot:
    def test_metadata_lists_the_finite_vocabularies(self, w):
        d = data(api(w["viewer"]).get(BASE + "scoring/metadata/"))
        assert {m["value"] for m in d["methods"]} == {"percentage", "raw_total", "weighted_sections"}
        assert "numeric" in d["scorable_types"] and "text" not in d["scorable_types"] and d["precision"] == {"min": 0, "max": 4}

    def test_metadata_needs_authentication(self):
        assert api().get(BASE + "scoring/metadata/").status_code in (401, 403)

    def test_the_api_root_lists_it(self, w):
        assert "scoring_metadata" in data(api(w["viewer"]).get(BASE))["endpoints"]


@pytest.mark.django_db
class TestConfigurationEndpoint:
    def draft(self, w):
        from apps.checklists import lifecycle

        return lifecycle.create_draft(w["etemplate"], actor=w["admin"], change_summary="s")

    def url(self, version, suffix=""):
        return f"{BASE}checklist-versions/{version.pk}/scoring/{suffix}"

    def test_a_designer_saves_reads_validates_and_previews(self, w):
        draft = self.draft(w)
        c = api(w["designer"])
        assert data(c.get(self.url(draft)))["configured"] is False
        r = c.put(self.url(draft), {"settings": {"precision": 1}, "document": EXC_DOC, "notes": "v1"}, format="json")
        assert r.status_code == 200 and data(r)["settings"]["precision"] == 1 and data(r)["editable"] and data(r)["issues"] == []
        assert data(c.post(self.url(draft, "validate/"), {}, format="json"))["ok"] is True
        preview = data(c.post(self.url(draft, "preview/"), {"answers": GOOD}, format="json"))
        assert preview["valid"] and preview["result"]["percentage"] == "100" and preview["result"]["items"][0]["code"] == "FIRE-EXIT"
        assert not ScoreResult.objects.exists()  # a preview stores nothing and creates no execution

    def test_preview_uses_a_candidate_configuration_and_reports_problems(self, w):
        draft = self.draft(w)
        c = api(w["designer"])
        bad = data(c.post(self.url(draft, "preview/"), {"document": {"items": {"FIRE-EXIT": {"points": {"yes": "1"}}}}, "answers": {}}, format="json"))
        assert bad["valid"] is False and bad["result"] is None and bad["issues"][0]["level"] == "error"
        ok = data(c.post(self.url(draft, "preview/"), {"document": EXC_DOC, "answers": {"FIRE-EXIT": "no"}, "final": False}, format="json"))
        assert ok["result"]["status"] == "provisional"

    def test_preview_rejects_unknown_questions_and_unknown_fields(self, w):
        draft = self.draft(w)
        c = api(w["designer"])
        assert c.post(self.url(draft, "preview/"), {"document": EXC_DOC, "answers": {"NOPE": "x"}}, format="json").status_code == 400
        assert c.post(self.url(draft, "preview/"), {"score": 100}, format="json").status_code == 400

    @pytest.mark.parametrize("body", [{"settings": {"precision": 9}}, {"document": {"eval": "1"}}, {"document": {"items": {"A": {"weight": 1.5}}}},
                                      {"score": 100}, {"settings": {"enabled": "yes"}}])
    def test_malformed_input_is_a_400(self, w, body):
        assert api(w["designer"]).put(self.url(self.draft(w)), body, format="json").status_code == 400

    def test_a_published_version_is_read_only_409(self, w):
        v = publish_scored(w)
        c = api(w["designer"])
        r = c.put(self.url(v), {"settings": {"precision": 0}}, format="json")
        assert r.status_code == 409 and not data(c.get(self.url(v)))["editable"]
        assert c.delete(self.url(v)).status_code == 409

    def test_only_scoring_designers_may_write_and_viewing_a_checklist_is_not_enough(self, w):
        draft = self.draft(w)
        for who in ("viewer", "score_viewer", "exec_viewer"):
            c = api(w[who])
            assert c.put(self.url(draft), {"settings": {"precision": 1}}, format="json").status_code in (403, 404)
            assert c.post(self.url(draft, "preview/"), {"document": EXC_DOC}, format="json").status_code in (403, 404)

    def test_reading_the_configuration_needs_scoring_view(self, w, org_a, make_user, assign):
        from apps.permissions import services as rbac
        from apps.permissions.registry import P

        draft = self.draft(w)
        reader = make_user(email="sc-reader@example.com", organization=org_a, role=None)
        assign(reader, org_a, rbac.create_role(organization=org_a, name="Checklist reader", permissions=[P.CHECKLIST_VIEW]))
        assert api(reader).get(f"{BASE}checklist-versions/{draft.pk}/").status_code == 200
        assert api(reader).get(self.url(draft)).status_code == 403  # may see the checklist, not its scoring rules
        assert api(w["designer"]).get(self.url(draft)).status_code == 200

    def test_another_organization_gets_404_and_nothing_leaks(self, w, make_user, org_b):
        draft = self.draft(w)
        outsider = make_user(email="sc-api-outsider@example.com", organization=org_b)
        c = api(outsider)
        assert c.get(self.url(draft)).status_code == 404 and c.put(self.url(draft), {}, format="json").status_code == 404
        assert c.post(self.url(draft, "preview/"), {}, format="json").status_code == 404

    def test_unauthenticated_and_malformed_ids(self, w):
        draft = self.draft(w)
        assert api().get(self.url(draft)).status_code in (401, 403)
        assert api(w["designer"]).get(f"{BASE}checklist-versions/not-a-uuid/scoring/").status_code == 404


@pytest.mark.django_db
class TestExecutionScoreEndpoints:
    def url(self, ex, suffix=""):
        return f"{BASE}executions/{ex.pk}/score/{suffix}"

    def test_a_score_viewer_sees_the_summary_but_not_the_breakdown(self, w, scored):
        _v, ex = scored
        c = api(w["score_viewer"])
        d = data(c.get(self.url(ex)))
        assert (d["stored"], d["status"], d["percentage"], d["grade"]) == (True, "final", "100", "A") and "items" not in d
        assert c.get(self.url(ex, "breakdown/")).status_code == 403
        assert c.get(self.url(ex, "history/")).status_code == 403

    def test_a_breakdown_viewer_sees_the_explained_breakdown(self, w, scored):
        _v, ex = scored
        d = data(api(w["breakdown_viewer"]).get(self.url(ex, "breakdown/")))
        assert d["items"][0]["explanation"] and d["sections"] and d["explanation"] and d["algorithm"] == 1
        assert len(data(api(w["breakdown_viewer"]).get(self.url(ex, "history/")))["results"]) == 1

    def test_seeing_the_execution_is_not_seeing_its_score(self, w, scored):
        _v, ex = scored
        assert api(w["exec_viewer"]).get(f"{BASE}executions/{ex.pk}/").status_code == 200
        assert api(w["exec_viewer"]).get(self.url(ex)).status_code == 403

    def test_cross_organization_and_scope_are_enforced(self, w, scored, make_user, org_b):
        _v, ex = scored
        assert api(make_user(email="sc-x@example.com", organization=org_b)).get(self.url(ex)).status_code == 404
        assert api(w["score_viewer"]).get(f"{BASE}executions/00000000-0000-0000-0000-000000000000/score/").status_code == 404
        assert api().get(self.url(ex)).status_code in (401, 403)

    def test_an_unscored_version_says_not_applicable(self, w):
        ex = run(w, w["eversion"], GOOD)
        d = data(api(w["score_viewer"]).get(self.url(ex)))
        assert d == {"applicable": False, "reason": "not_configured"}

    def test_an_execution_in_progress_shows_a_provisional_score_to_those_who_may_preview(self, w):
        v = publish_scored(w)
        ex = run(w, v, {"FIRE-EXIT": "yes"}, submit=False)
        assert api(w["score_viewer"]).get(self.url(ex)).status_code == 403
        d = data(api(w["previewer"]).get(self.url(ex)))
        assert (d["stored"], d["status"]) == (False, "provisional") and not ScoreResult.objects.exists()

    def test_nothing_writes_a_score(self, w, scored):
        _v, ex = scored
        c = api(w["recalculator"])
        for method in (c.put, c.patch, c.delete):
            assert method(self.url(ex), {"percentage": 100}, format="json").status_code == 405
        r = c.post(self.url(ex, "recalculate/"), {"reason": "x", "percentage": 5, "grade": "A"}, format="json")
        assert r.status_code == 400  # client-supplied totals are refused, not ignored
        assert selectors.current_result(ex).percentage == 100

    def test_recalculation_needs_permission_and_a_reason(self, w, scored):
        _v, ex = scored
        assert api(w["breakdown_viewer"]).post(self.url(ex, "recalculate/"), {"reason": "x"}, format="json").status_code == 403
        c = api(w["recalculator"])
        assert c.post(self.url(ex, "recalculate/"), {}, format="json").status_code == 400
        d = data(c.post(self.url(ex, "recalculate/"), {"reason": "Quarterly check"}, format="json"))
        assert d["changed"] is False and d["status"] == "final"
        assert ScoreResult.objects.filter(execution=ex).count() == 1
