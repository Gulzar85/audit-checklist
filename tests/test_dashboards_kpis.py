"""Phase 20 KPI accuracy, comparisons, filters and scope - with explicit numerators and denominators."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.dashboards import catalog, kpis, policies
from apps.dashboards.services import layouts, metrics
from apps.reporting import registry as report_registry

from dashboards_support import build_exec_world, build_quality_world, seed_executions


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_exec_world(org_a, org_b, make_user, assign, make_type)


@pytest.fixture
def seeded(w):
    return seed_executions(w)  # r1: 1 submitted + 1 in progress; r2: 2 submitted (one with a critical failure)


def ctx(**raw):
    return metrics.clean_context(raw)


def data(w, widget, who="dash", **raw):
    return metrics.widget_data(w[who], w["org"], widget, ctx(**raw))


def today():
    return timezone.localdate()


# ------------------------------------------------------------------------------------------------------------------------------ registry
class TestRegistries:
    def test_every_kpi_and_widget_points_at_a_real_report_and_metric(self):
        for kpi in kpis.REGISTRY.values():
            report = report_registry.REGISTRY[kpi.report]
            assert kpi.metric in report.column_map, kpi.code
            assert not kpi.group_by or kpi.group_by in {v for v, _ in report.group_by}, kpi.code
            assert not kpi.drill_report or kpi.drill_report in report_registry.REGISTRY
        for widget in catalog.WIDGETS.values():
            if widget.kind != catalog.KPI:
                report = report_registry.REGISTRY[widget.report]
                assert all(m.key in report.column_map for m in widget.metrics), widget.code
                assert widget.group_by in ("", "period") or widget.group_by in {v for v, _ in report.group_by}, widget.code
                assert not widget.drill_key or widget.drill_key in {f.key for f in report_registry.REGISTRY[widget.drill_report].filters}, widget.code

    def test_every_dashboard_lists_known_widgets(self):
        assert {"overview", "operations", "scores", "findings", "actions", "workflows", "evaluations", "locations"} <= set(catalog.DASHBOARDS)
        for dashboard in catalog.DASHBOARDS.values():
            assert all(code in catalog.WIDGETS for code in dashboard.widgets)

    def test_period_change_is_not_invented_for_a_zero_or_missing_baseline(self):
        assert metrics.change(10, 5) == 100.0 and metrics.change(5, 10) == -50.0 and metrics.change(3, 3) == 0.0
        assert metrics.change(5, 0) is None and metrics.change(5, None) is None and metrics.change(None, 5) is None


# ------------------------------------------------------------------------------------------------------------------------------ filters
class TestFilterContext:
    @pytest.mark.parametrize("raw,code", [({"nope": 1}, "unsupported_filter"), ({"date_from": "2026-13-01"}, "invalid_filter"), ({"location": "x"}, "invalid_filter"),
                                          ({"template": 5}, "invalid_filter"), ({"bucket": "hour"}, "invalid_filter"), ({"include_descendants": "yes"}, "invalid_filter"),
                                          ({"date_from": "2026-02-01", "date_to": "2026-01-01"}, "invalid_range"), ({"date_from": "2024-01-01", "date_to": "2026-01-01"}, "range_too_long"),
                                          ({"status__in": "x"}, "unsupported_filter")])
    def test_invalid_contexts_are_refused(self, raw, code):
        with pytest.raises(metrics.DashboardError) as info:
            metrics.clean_context(raw)
        assert info.value.code == code

    def test_dates_are_inclusive_and_previous_period_has_the_same_length(self):
        c = ctx(date_from="2026-03-01", date_to="2026-03-31")
        assert metrics.period_of(c) == (date(2026, 3, 1), date(2026, 3, 31))
        assert metrics.previous_range(c) == ("2026-01-29", "2026-02-28")  # 31 days immediately before

    def test_automatic_buckets(self):
        assert metrics.bucket_for(ctx(date_from="2026-03-01", date_to="2026-03-31")) == "day"
        assert metrics.bucket_for(ctx(date_from="2026-01-01", date_to="2026-04-30")) == "week"
        assert metrics.bucket_for(ctx(date_from="2026-01-01", date_to="2026-12-31")) == "month"
        assert metrics.bucket_for(ctx(bucket="quarter")) == "quarter"


# ------------------------------------------------------------------------------------------------------------------------------ accuracy (checklists, scores, evaluations)
@pytest.mark.django_db
class TestChecklistKpis:
    def test_assignment_kpis_have_explicit_numerators_and_denominators(self, w, seeded):
        # eligible = 4 issued, not cancelled; completed = 3 (submitted executions complete their assignment); in progress = 1; not started = 0
        assert data(w, "kpi_assignments_eligible")["value"] == 4
        assert data(w, "kpi_assignments_completed")["value"] == 3
        assert data(w, "kpi_completion_rate")["value"] == 75.0  # 3 / 4
        assert data(w, "kpi_assignments_pending")["value"] == 0
        assert data(w, "kpi_assignments_overdue")["value"] == 0
        assert data(w, "kpi_executions_submitted")["value"] == 3

    def test_cancelled_assignments_leave_the_denominator_and_overdue_follows_the_lifecycle(self, w, seeded):
        from apps.assignments.models import ChecklistAssignment
        done = ChecklistAssignment.objects.filter(status="completed").first()
        ChecklistAssignment.objects.filter(pk=done.pk).update(status="cancelled", cancelled_at=timezone.now(), cancelled_by=w["admin"], cancel_reason="t", due_at=timezone.now() - timedelta(days=3))
        ChecklistAssignment.objects.filter(status="in_progress").update(due_at=timezone.now() - timedelta(days=1))
        assert data(w, "kpi_assignments_eligible")["value"] == 3
        assert data(w, "kpi_completion_rate")["value"] == pytest.approx(66.7)  # 2 / 3
        assert data(w, "kpi_assignments_overdue")["value"] == 1  # the cancelled one is not overdue

    def test_empty_scope_gives_zero_counts_and_no_percentages(self, w):
        assert data(w, "kpi_assignments_eligible")["value"] == 0
        rate = data(w, "kpi_completion_rate")
        assert rate["value"] is None and rate["status"] == "empty"  # 0 / 0 is not 0%
        assert data(w, "kpi_average_score")["status"] == "empty"

    def test_previous_period_comparison(self, w, seeded):
        d = data(w, "kpi_executions_submitted", date_from=today().isoformat(), date_to=today().isoformat())
        assert d["value"] == 3 and d["previous"] == 0 and d["change_pct"] is None  # zero baseline: no percentage
        now_metric = data(w, "kpi_findings_open")
        assert now_metric["previous"] is None and "current state" in now_metric["comparison"]

    def test_average_score_is_only_shown_for_one_checklist(self, w, seeded):
        from apps.scoring.models import ScoreResult
        avg = data(w, "kpi_average_score")
        stored = [float(p) for p in ScoreResult.objects.filter(is_current=True, status="final").values_list("percentage", flat=True)]
        assert avg["status"] == "ok" and avg["value"] == pytest.approx(round(sum(stored) / len(stored), 2))  # unweighted mean of stored percentages
        version = seeded[0]
        assert data(w, "kpi_average_score", template=str(version.template_id))["value"] == avg["value"]

    def test_several_checklists_are_not_comparable(self, w, monkeypatch):
        # stored results are append-only, so the second checklist is simulated at the report boundary: two template groups in scope
        monkeypatch.setattr(metrics, "_rows", lambda *a, **k: [{"group": "A", "key": "a", "average": 80.0}, {"group": "B", "key": "b", "average": 40.0}])
        d = data(w, "kpi_average_score")
        assert d["status"] == "not_comparable" and d["value"] is None and "2 checklists" in d["note"]

    def test_unscored_executions_are_not_zero_scores(self, w, seeded):
        dist = data(w, "score_distribution")
        assert sum(s["values"]["scored"] for s in dist["series"]) == 3  # the in-progress execution is not in any band

    def test_critical_failures_and_grades(self, w, seeded):
        assert data(w, "kpi_critical_failures")["value"] == 1
        grades = data(w, "grade_distribution")
        assert sum(s["values"]["scored"] for s in grades["series"]) == 3

    def test_evaluation_distribution_uses_the_engines_outcomes(self, w, seeded):
        from apps.evaluation.constants import Outcome
        from apps.evaluation.models import EvaluationRun
        d = data(w, "evaluation_outcomes")
        assert {s["key"] for s in d["series"]} <= set(Outcome.values)
        assert sum(s["values"]["runs"] for s in d["series"]) == EvaluationRun.objects.filter(is_current=True).count()
        attention = EvaluationRun.objects.filter(is_current=True, outcome__in=["fail", "review_required"]).count()
        assert data(w, "kpi_evaluations_attention")["value"] == attention

    def test_trends_fill_empty_buckets_with_zero_counts_and_blank_averages(self, w, seeded):
        start = (today() - timedelta(days=6)).isoformat()
        trend = data(w, "execution_trend", date_from=start, date_to=today().isoformat())
        assert trend["bucket"] == "day" and len(trend["series"]) == 7
        assert [s["values"]["started"] for s in trend["series"]][:6] == [0] * 6 and trend["series"][-1]["values"]["started"] == 4
        scores = data(w, "score_trend", date_from=start, date_to=today().isoformat())
        assert [s["values"]["average"] for s in scores["series"]][:6] == [None] * 6 and scores["series"][-1]["values"]["average"] is not None

    def test_location_comparison_and_drill_downs(self, w, seeded):
        table = data(w, "location_comparison")
        rows = {s["label"]: s["values"] for s in table["series"]}
        assert rows["Restaurant 1"]["eligible"] == 2 and rows["Restaurant 2"]["submitted"] == 2 and rows["Warehouse 1"]["avg_score"] is None
        by_status = data(w, "execution_status")
        submitted = next(s for s in by_status["series"] if s["key"] == "submitted")
        assert submitted["values"]["started"] == 3 and "/reports/executions/" in submitted["drill"] and "f_status=submitted" in submitted["drill"]

    def test_filters_a_widget_cannot_use_are_reported_not_widened(self, w, seeded):
        version = seeded[0]
        d = data(w, "location_comparison", template=str(version.template_id))
        assert "template" in d["not_applied"]
        e = data(w, "execution_status", template=str(version.template_id))
        assert e["not_applied"] == [] and sum(s["values"]["started"] for s in e["series"]) == 4


# ------------------------------------------------------------------------------------------------------------------------------ scope and permissions
@pytest.mark.django_db
class TestScope:
    def test_location_scoped_users_get_scoped_totals(self, w, seeded):
        assert data(w, "kpi_assignments_eligible", who="dash_r1")["value"] == 2  # r1 only: 1 completed + 1 in progress
        assert data(w, "kpi_completion_rate", who="dash_r1")["value"] == 50.0
        assert {s["label"] for s in data(w, "location_comparison", who="dash_r1")["series"]} <= {"Restaurant 1"}
        assert data(w, "kpi_assignments_eligible", who="dash_r1", location=str(w["r2"].pk))["value"] == 0  # a filter cannot widen scope

    def test_location_filter_with_and_without_descendants(self, w, seeded):
        assert data(w, "kpi_assignments_eligible", location=str(w["area1"].pk))["value"] == 4
        assert data(w, "kpi_assignments_eligible", location=str(w["area1"].pk), include_descendants=False)["value"] == 0
        assert data(w, "kpi_assignments_eligible", location=str(w["r2"].pk))["value"] == 2

    def test_dashboard_view_is_required_and_other_organizations_see_nothing(self, w, seeded):
        with pytest.raises(PermissionDenied):
            data(w, "kpi_assignments_eligible", who="dash_noview")
        with pytest.raises(PermissionDenied):
            data(w, "kpi_assignments_eligible", who="dash_other")  # not their organization
        other_org = w["dash_other"].organization_memberships.first().organization
        assert metrics.widget_data(w["dash_other"], other_org, "kpi_assignments_eligible", ctx())["value"] == 0

    def test_widgets_need_their_source_permission(self, w, seeded):
        with pytest.raises(PermissionDenied):
            data(w, "kpi_average_score", who="dash_findings_only")
        names = {d.code for d in policies.visible_dashboards(w["dash_findings_only"], w["org"])}
        assert "findings" in names and "scores" not in names and "operations" not in names

    def test_unknown_widgets_are_refused(self, w):
        for code in ("nope", "", None, "kpi_", "__import__"):
            with pytest.raises((metrics.DashboardError, PermissionDenied)):
                metrics.widget_data(w["dash"], w["org"], code, ctx())


# ------------------------------------------------------------------------------------------------------------------------------ layouts
@pytest.mark.django_db
class TestLayouts:
    def test_save_load_reset_and_validation(self, w, seeded):
        dashboard = catalog.DASHBOARDS["overview"]
        assert layouts.effective(w["dash"], w["org"], dashboard)["source"] == "default"
        first, second = dashboard.widgets[0], dashboard.widgets[1]
        layouts.save(w["dash"], w["org"], dashboard, [{"code": second, "hidden": False}, {"code": first, "hidden": True}])
        eff = layouts.effective(w["dash"], w["org"], dashboard)
        assert eff["source"] == "personal" and eff["widgets"][0] == {"code": second, "hidden": False} and eff["widgets"][1] == {"code": first, "hidden": True}
        assert len(eff["widgets"]) == len(dashboard.widgets)  # the rest is appended visible
        for bad in ([{"code": "nope"}], [{"code": first}, {"code": first}], [{"code": first, "hidden": "x"}], [{"code": first, "html": "<script>"}], "x", [1]):
            with pytest.raises(layouts.LayoutError):
                layouts.save(w["dash"], w["org"], dashboard, bad)
        layouts.reset(w["dash"], w["org"], dashboard)
        assert layouts.effective(w["dash"], w["org"], dashboard)["source"] == "default"

    def test_shared_defaults_need_manage_and_never_grant_widgets(self, w, seeded):
        dashboard = catalog.DASHBOARDS["overview"]
        with pytest.raises(PermissionDenied):
            layouts.save(w["dash"], w["org"], dashboard, [], shared=True)
        layouts.save(w["dash_manager"], w["org"], dashboard, [{"code": "kpi_average_score", "hidden": False}], shared=True)
        assert layouts.effective(w["dash"], w["org"], dashboard)["source"] == "organization"
        limited = layouts.effective(w["dash_findings_only"], w["org"], dashboard)["widgets"]
        assert "kpi_average_score" not in {i["code"] for i in limited}  # a shared layout cannot show a widget the viewer may not use

    def test_a_tampered_stored_layout_is_repaired_on_load(self, w):
        from apps.dashboards.models import DashboardLayout
        dashboard = catalog.DASHBOARDS["overview"]
        DashboardLayout.objects.create(organization=w["org"], user=w["dash"], dashboard="overview", widgets=[{"code": "evil", "hidden": False}, "junk", {"code": dashboard.widgets[0]}])
        codes = [i["code"] for i in layouts.effective(w["dash"], w["org"], dashboard)["widgets"]]
        assert "evil" not in codes and codes[0] == dashboard.widgets[0] and set(codes) == set(dashboard.widgets)


# ------------------------------------------------------------------------------------------------------------------------------ quality world
@pytest.fixture
def q(org_a, org_b, make_user, assign, make_type):
    return build_quality_world(org_a, org_b, make_user, assign, make_type)


@pytest.mark.django_db
class TestQualityKpis:
    def test_findings_and_actions_follow_their_own_lifecycles(self, q):
        from actions_support import make_action, make_finding, to_status
        from apps.corrective_actions.models import CorrectiveAction
        from apps.findings.models import Finding, FindingSeverity

        f1, f2 = make_finding(q, title="F1"), make_finding(q, title="F2", severity="medium")
        submitted = to_status(q, make_action(q, f1), "submitted")
        verified = to_status(q, make_action(q, f1, title="V"), "verified")
        overdue = make_action(q, f2, title="O")
        CorrectiveAction.objects.filter(pk=overdue.pk).update(due_at=timezone.now() - timedelta(days=1))
        dq = lambda code, **raw: metrics.widget_data(q["dash"], q["org"], code, ctx(**raw))  # noqa: E731
        open_findings = Finding.objects.filter(status__in=["open", "acknowledged", "in_progress", "reopened"]).count()
        assert dq("kpi_findings_open")["value"] == open_findings == 2  # a verified action does not close its finding
        assert dq("kpi_findings_created")["value"] == 2
        assert dq("kpi_actions_awaiting_verification")["value"] == 1  # submitted, not verified
        assert dq("kpi_actions_overdue")["value"] == 1
        assert dq("kpi_actions_open")["value"] == CorrectiveAction.objects.filter(status__in=["open", "in_progress", "blocked", "rejected"]).count()
        top = FindingSeverity.objects.filter(organization=q["org"], is_active=True).order_by("-rank").first()
        expected_top = Finding.objects.filter(severity=top, status__in=["open", "acknowledged", "in_progress", "reopened"]).count()
        t = dq("kpi_findings_top_severity")
        assert t["value"] == expected_top and t["note"] == top.name
        severities = {s["label"]: s["values"]["open_now"] for s in dq("findings_by_severity")["series"]}
        assert sum(severities.values()) == 2
        statuses = {s["key"]: s["values"]["created"] for s in dq("actions_by_status")["series"]}
        assert statuses.get("submitted") == 1 and statuses.get("verified") == 1
        assert submitted.status == "submitted" and verified.status == "verified"

    def test_workflow_backlog(self, q):
        from apps.workflows import services as wf
        from apps.workflows.models import WorkflowTask
        from workflows_support import ca_workflow, submitted_action

        ca_workflow(q)
        action = submitted_action(q)
        if not action_workflow_started(action):
            wf.start(actor=None, target_type="corrective_action", target=action, definition_code="ca-verification", system=True)
        open_tasks = WorkflowTask.objects.filter(status__in=["pending", "claimed"], instance__status="active").count()
        assert open_tasks >= 1
        d = metrics.widget_data(q["dash"], q["org"], "kpi_workflow_tasks_open", ctx())
        assert d["value"] == open_tasks
        WorkflowTask.objects.filter(status__in=["pending", "claimed"]).update(due_at=timezone.now() - timedelta(hours=1))
        assert metrics.widget_data(q["dash"], q["org"], "kpi_workflow_tasks_overdue", ctx())["value"] == open_tasks


def action_workflow_started(action):
    from apps.workflows.models import WorkflowInstance
    return WorkflowInstance.objects.filter(target_id=action.pk).exists()


@pytest.mark.django_db
def test_dashboard_queries_are_bounded(w, seeded, django_assert_max_num_queries):
    context = ctx()
    for code in catalog.DASHBOARDS["overview"].widgets:
        with django_assert_max_num_queries(60):
            metrics.widget_data(w["dash"], w["org"], code, context)
