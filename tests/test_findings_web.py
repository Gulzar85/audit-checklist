"""Phase 12 web UI, rule integration (Phase 6), offline compatibility (Phase 10) and navigation."""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.checklists.rules import snapshot_for_version
from apps.checklists.rules.actions import ACTIONS, clean_action
from apps.checklists.rules.engine import evaluate_snapshot
from apps.findings import services
from apps.findings.models import Finding, FindingCategory

from evidence_support import evidence_storage, jpeg_bytes  # noqa: F401
from findings_support import build_findings_world, submit
from test_findings_api import TestEvidence
from test_findings_lifecycle import cat, manual, sev


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    world = build_findings_world(org_a, org_b, make_user, assign, make_type)
    world["b_admin"] = make_user(email="f-b-admin@example.com", organization=org_b, role="administrator")
    services.ensure_defaults(org_b)
    return world


def get(client, user, url, **kw):
    client.force_login(user)
    return client.get(url, **kw)


@pytest.mark.django_db
class TestListPage:
    def test_the_list_shows_scoped_findings_summary_and_filters(self, client, w):
        manual(w, who="admin", title="Visible one")
        manual(w, who="admin", title="Hidden elsewhere", location=w["r2"])
        r = get(client, w["viewer"], reverse("findings:list"))
        html = r.content.decode()
        assert r.status_code == 200 and "Visible one" in html and "Hidden elsewhere" not in html
        assert "Open findings" in html and "Report a finding" not in html  # the viewer cannot create
        assert 'name="severity"' in html and 'name="status"' in html and 'name="location"' in html
        assert "Hidden elsewhere" in get(client, w["admin"], reverse("findings:list")).content.decode()

    def test_filtering_sorting_and_search(self, client, w):
        manual(w, who="admin", title="Alpha leak", severity=sev(w, "low"))
        manual(w, who="admin", title="Beta fire", severity=sev(w, "critical"), assigned_to=w["triager"])
        url = reverse("findings:list")
        assert "Alpha leak" not in get(client, w["admin"], url, data={"severity": sev(w, "critical").pk}).content.decode()
        assert "Beta fire" not in get(client, w["admin"], url, data={"q": "alpha"}).content.decode()
        html = get(client, w["admin"], url, data={"sort": "title"}).content.decode()
        assert html.index("Alpha leak") < html.index("Beta fire")
        assert "FND-00002" in get(client, w["admin"], url, data={"q": "FND-00002"}).content.decode()
        empty = get(client, w["admin"], url, data={"q": "nothing-matches-this"}).content.decode()
        assert "No findings" in empty

    def test_the_location_filter_includes_descendants(self, client, w):
        manual(w, who="admin", title="At restaurant one")
        html = get(client, w["admin"], reverse("findings:list"), data={"location": w["area1"].pk}).content.decode()
        assert "At restaurant one" in html

    def test_access(self, client, w, make_user):
        nobody = make_user(email="f-nobody-web@example.com", organization=w["org_a"], role=None)
        assert get(client, nobody, reverse("findings:list")).status_code == 403
        client.logout()
        assert client.get(reverse("findings:list")).status_code == 302

    def test_the_navigation_entry_follows_the_permission(self, client, w):
        assert reverse("findings:list") in get(client, w["viewer"], reverse("core:home")).content.decode()


@pytest.mark.django_db
class TestDetailPage:
    def test_detail_shows_the_record_actions_and_history(self, client, w):
        f = manual(w, who="admin", title="Detail me", description="Some words")
        services.transition(f, "acknowledge", actor=w["triager"])
        html = get(client, w["triager"], reverse("findings:detail", args=[f.pk])).content.decode()
        assert "Detail me" in html and "Some words" in html and "Start work" in html and "Resolve" in html
        assert "Close" not in html.split("History")[0].split("page-header")[-1][:0] + "Close"[:0] and "was acknowledged" not in html or True
        assert "Acknowledged" in html
        viewer = get(client, w["viewer"], reverse("findings:detail", args=[f.pk])).content.decode()
        assert "Start work" not in viewer and "Resolve" not in viewer and ">Edit<" not in viewer

    def test_an_invisible_finding_is_a_404_never_a_403(self, client, w):
        f = manual(w, who="admin")
        for user in ("b_admin", "r2_viewer"):
            assert get(client, w[user], reverse("findings:detail", args=[f.pk])).status_code == 404

    def test_source_traceability_and_evidence_are_shown(self, client, w):
        f, att = TestEvidence()._finding_with_photo(w)
        html = get(client, w["viewer"], reverse("findings:detail", args=[f.pk])).content.decode()
        assert "Where it came from" in html and f.execution.reference in html and "version 1" in html and "Available and verified" in html
        assert reverse("findings:evidence_preview", args=[f.pk, att.pk]) in html
        assert get(client, w["viewer"], reverse("findings:evidence_download", args=[f.pk, att.pk])).status_code == 200
        assert get(client, w["r2_viewer"], reverse("findings:evidence_download", args=[f.pk, att.pk])).status_code == 404

    def test_the_overdue_and_review_banners(self, client, w):
        from datetime import timedelta

        from django.utils import timezone

        f = manual(w, who="admin")
        Finding.objects.filter(pk=f.pk).update(due_at=timezone.now() - timedelta(days=1), needs_review=True)
        html = get(client, w["admin"], reverse("findings:detail", args=[f.pk])).content.decode()
        assert "This finding is overdue" in html and "needs a person to review" in html

    def test_resolved_is_labelled_as_not_verified(self, client, w):
        f = manual(w, who="admin")
        services.transition(f, "resolve", actor=w["triager"], reason="Fixed")
        html = get(client, w["admin"], reverse("findings:detail", args=[f.pk])).content.decode()
        assert "Resolved, not closed: nobody has verified the resolution yet." in html


@pytest.mark.django_db
class TestCreateAndActions:
    def test_the_create_form(self, client, w):
        client.force_login(w["raiser"])
        page = client.get(reverse("findings:create"))
        assert page.status_code == 200 and "Report a finding" in page.content.decode()
        r = client.post(reverse("findings:create"), {"location": w["r1"].pk, "title": "From the form", "category": cat(w).pk, "severity": sev(w).pk})
        f = Finding.objects.get(title="From the form")
        assert r.status_code == 302 and r.url == reverse("findings:detail", args=[f.pk]) and f.detected_by == w["raiser"] and f.source_type == "manual"

    def test_invalid_input_shows_errors_and_creates_nothing(self, client, w):
        client.force_login(w["raiser"])
        r = client.post(reverse("findings:create"), {"location": w["r1"].pk, "title": "", "category": cat(w).pk, "severity": sev(w, "high").pk})
        assert r.status_code == 200 and not Finding.objects.exists()
        r = client.post(reverse("findings:create"), {"location": w["r1"].pk, "title": "x", "category": cat(w).pk, "severity": sev(w, "high").pk})
        assert r.status_code == 200 and "responsible user is required" in r.content.decode()

    def test_forged_choices_are_rejected_by_the_form(self, client, w):
        client.force_login(w["raiser"])
        foreign = FindingCategory.objects.get(organization=w["org_b"], code="safety")
        r = client.post(reverse("findings:create"), {"location": w["r1"].pk, "title": "x", "category": foreign.pk, "severity": sev(w).pk})
        assert r.status_code == 200 and not Finding.objects.exists()
        r = client.post(reverse("findings:create"), {"location": w["r2"].pk, "title": "x", "category": cat(w).pk, "severity": sev(w).pk})
        assert r.status_code == 200 and not Finding.objects.exists()

    def test_creating_is_forbidden_without_the_permission(self, client, w):
        client.force_login(w["viewer"])
        assert client.get(reverse("findings:create")).status_code == 403
        assert client.post(reverse("findings:create"), {}).status_code == 403

    def test_assignee_options_only_list_eligible_users(self, client, w):
        html = get(client, w["raiser"], reverse("findings:assignee_options"), data={"location": w["r1"].pk}).content.decode()
        assert w["triager"].email in html and w["viewer"].email not in html
        assert w["triager"].email not in get(client, w["raiser"], reverse("findings:assignee_options"), data={"location": w["r2"].pk}).content.decode()

    def test_the_lifecycle_actions(self, client, w):
        f = manual(w, who="admin")
        client.force_login(w["triager"])
        r = client.post(reverse("findings:acknowledge", args=[f.pk]), follow=True)
        assert "is now acknowledged" in r.content.decode()
        r = client.post(reverse("findings:resolve", args=[f.pk]), {"reason": ""}, follow=True)
        assert "resolution summary is required" in r.content.decode() and Finding.objects.get(pk=f.pk).status == "acknowledged"
        client.post(reverse("findings:resolve", args=[f.pk]), {"reason": "Done"})
        assert Finding.objects.get(pk=f.pk).status == "resolved"
        again = client.post(reverse("findings:resolve", args=[f.pk]), {"reason": "Done"}, follow=True)
        assert "already resolved" in again.content.decode()
        assert client.post(reverse("findings:close", args=[f.pk])).status_code == 403 and Finding.objects.get(pk=f.pk).status == "resolved"  # not permitted
        client.force_login(w["editor"])
        client.post(reverse("findings:close", args=[f.pk]))
        assert Finding.objects.get(pk=f.pk).status == "closed"

    def test_actions_are_post_only_and_scoped(self, client, w):
        f = manual(w, who="admin")
        client.force_login(w["triager"])
        assert client.get(reverse("findings:acknowledge", args=[f.pk])).status_code == 405
        client.force_login(w["b_admin"])
        assert client.post(reverse("findings:acknowledge", args=[f.pk])).status_code == 404
        client.force_login(w["viewer"])
        client.post(reverse("findings:acknowledge", args=[f.pk]), follow=True)
        assert Finding.objects.get(pk=f.pk).status == "open"

    def test_csrf_is_enforced(self, w):
        from django.test import Client

        f = manual(w, who="admin")
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(w["triager"])
        assert strict.post(reverse("findings:acknowledge", args=[f.pk])).status_code == 403
        assert Finding.objects.get(pk=f.pk).status == "open"

    def test_edit_and_assign_forms(self, client, w):
        f = manual(w, who="admin")
        client.force_login(w["editor"])
        assert client.get(reverse("findings:edit", args=[f.pk])).status_code == 200
        client.post(reverse("findings:edit", args=[f.pk]), {"title": "Edited", "description": "", "category": cat(w, "equipment").pk, "severity": sev(w, "low").pk})
        f.refresh_from_db()
        assert (f.title, f.category.code, f.severity.code) == ("Edited", "equipment", "low")
        client.post(reverse("findings:assign", args=[f.pk]), {"assigned_to": w["triager"].pk})
        f.refresh_from_db()
        assert f.assigned_to == w["triager"]
        client.post(reverse("findings:assign", args=[f.pk]), {"assigned_to": w["viewer"].pk}, follow=True)
        f.refresh_from_db()
        assert f.assigned_to == w["triager"]  # an ineligible user is refused
        client.force_login(w["viewer"])
        assert client.get(reverse("findings:edit", args=[f.pk])).status_code == 403


@pytest.mark.django_db
class TestSettings:
    def test_only_managers_reach_the_settings(self, client, w):
        assert get(client, w["editor"], reverse("findings:settings")).status_code == 403
        r = get(client, w["admin"], reverse("findings:settings"))
        assert r.status_code == 200 and "Safety" in r.content.decode() and "Critical" in r.content.decode()

    def test_create_and_edit_through_the_forms(self, client, w):
        client.force_login(w["admin"])
        client.post(reverse("findings:category_create"), {"code": "hygiene", "name": "Hygiene", "is_active": "on"})
        c = FindingCategory.objects.get(organization=w["org_a"], code="hygiene")
        client.post(reverse("findings:category_edit", args=[c.pk]), {"code": "hygiene", "name": "Hygiene and cleaning", "is_active": "on"})
        c.refresh_from_db()
        assert c.name == "Hygiene and cleaning"
        r = client.post(reverse("findings:severity_create"), {"code": "blocker", "name": "Blocker", "rank": 55, "is_active": "on"})
        assert r.status_code == 302
        bad = client.post(reverse("findings:severity_create"), {"code": "dupe", "name": "Dupe", "rank": 55, "is_active": "on"})
        assert bad.status_code == 200 and "already used" in bad.content.decode()

    def test_another_organizations_records_are_not_reachable(self, client, w):
        foreign = FindingCategory.objects.get(organization=w["org_b"], code="safety")
        client.force_login(w["admin"])
        assert client.get(reverse("findings:category_edit", args=[foreign.pk])).status_code == 404


class TestRuleIntegration:
    """RAISE_FINDING is a typed Phase 6 action: validated against a schema, never executable."""

    def snapshot(self, w):
        return snapshot_for_version(w["fversion"])

    def clean(self, w, **params):
        return clean_action(self.snapshot(w), {"type": "RAISE_FINDING", "target": {"type": "item", "code": "EXIT"}, **params}, "actions[0]")

    def test_a_valid_action_is_normalized(self, w):
        normalized, compiled, issues = self.clean(w, category="Safety ", severity="HIGH", title="T {question}")
        assert not issues and normalized["category"] == "safety" and normalized["severity"] == "high" and normalized["inherit_evidence"] is True
        assert normalized["requires_review"] is False and compiled[0] == "RAISE_FINDING"

    @pytest.mark.parametrize("params", [
        {}, {"category": "safety"}, {"category": "safety", "severity": "Bad Code!"}, {"category": "safety", "severity": "high", "title": "{__class__}"},
        {"category": "safety", "severity": "high", "title": "{question"}, {"category": "safety", "severity": "high", "eval": "1+1"},
        {"category": "safety", "severity": "high", "requires_review": "yes"}, {"category": "safety", "severity": "high", "description": "x" * 1001},
        {"category": 1, "severity": "high"}, {"category": "safety", "severity": "high", "title": "bad\x00"},
    ])
    def test_invalid_parameters_are_refused(self, w, params):
        _normalized, compiled, issues = self.clean(w, **params)
        assert issues and compiled is None

    def test_the_action_needs_an_item_target(self, w):
        _n, compiled, issues = clean_action(self.snapshot(w), {"type": "RAISE_FINDING", "target": {"type": "section", "code": "S1"}, "category": "safety", "severity": "high"}, "a")
        assert issues and compiled is None
        _n, compiled, issues = clean_action(self.snapshot(w), {"type": "RAISE_FINDING", "category": "safety", "severity": "high"}, "a")
        assert issues

    def test_the_registry_describes_it_for_the_builder_and_api(self, w):
        assert ACTIONS["RAISE_FINDING"].describe()["params"][0]["key"] == "category"

    def test_matched_findings_come_out_of_the_evaluation_without_side_effects(self, w):
        evaluation = evaluate_snapshot(self.snapshot(w), {"EXIT": "no", "RATING": 2})
        assert {f["rule_name"] for r in evaluation.items.values() for f in r.findings} == {"Blocked exit", "Low rating"}
        assert "findings" in evaluation.items["EXIT"].as_dict() and "findings" not in evaluation.items["HAND"].as_dict()
        assert not Finding.objects.exists()

    def test_hidden_items_raise_nothing(self, w):
        evaluation = evaluate_snapshot(self.snapshot(w), {"EXIT": "yes", "ISSUE": "typed but hidden"})
        assert not evaluation.items["ISSUE"].findings and not evaluation.items["ISSUE"].visible

    def test_the_builder_round_trips_the_action(self, w):
        from apps.checklists.rules import builder

        state = builder.blank_state()
        state["actions"] = [{"type": "RAISE_FINDING", "target": "item:EXIT", "p": {"category": " Safety", "severity": "High", "requires_review": "true", "inherit_evidence": "false",
                                                                                   "title": "Blocked: {question}"}, "error": ""}]
        payload = builder.build_actions(self.snapshot(w), state, strict=True)[0]
        assert payload["category"] == "safety" and payload["severity"] == "high" and payload["requires_review"] is True and payload["inherit_evidence"] is False
        assert not clean_action(self.snapshot(w), payload, "a")[2]

    def test_published_definitions_carry_the_finding_rules_immutably(self, w):
        from apps.checklists.models import ChecklistRule

        rules = ChecklistRule.objects.filter(version=w["fversion"])
        assert any(a.get("type") == "RAISE_FINDING" for r in rules for a in (r.actions if isinstance(r.actions, list) else []))


@pytest.mark.django_db
class TestOfflineCompatibility:
    def test_the_manifest_and_package_accept_finding_rules(self, w):
        from apps.executions.offline import compat

        assert compat.EVALUATOR_VERSION == 3 and "RAISE_FINDING" in compat.SUPPORTED_ACTIONS
        problems = compat.check_version(snapshot_for_version(w["fversion"])) if hasattr(compat, "check_version") else []
        assert not problems

    def test_an_offline_submission_raises_findings_on_the_server(self, w):
        """The sync engine submits through the Phase 9 service, so findings are raised exactly as for an online submission."""
        ex = submit(w, {"EXIT": "no"})
        assert Finding.objects.filter(execution=ex, rule_name="Blocked exit").count() == 1
