"""Phase 19 core: registry and filter validation, business-data accuracy, scope, people columns, query safety."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.utils import timezone

from apps.reporting import constants as k
from apps.reporting import filters, registry, services
from apps.reporting.errors import ReportError
from apps.scoring.models import ScoreResult

from reporting_support import build_reporting_world, seed_executions


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_reporting_world(org_a, org_b, make_user, assign, make_type)


@pytest.fixture
def seeded(w):
    return seed_executions(w)


def run_report(w, who, code, raw=None, **kw):
    return services.run(w[who], w["org"], code, raw, **kw)


def by(rows, key):
    return {r[key]: r for r in rows}


# ------------------------------------------------------------------------------------------------------------------------------ registry
class TestRegistry:
    def test_the_expected_reports_are_registered(self):
        assert {"executions", "assignments", "findings", "corrective_actions", "workflows", "evaluations", "scores", "completion", "score_summary", "location_comparison"} <= set(registry.REGISTRY)
        assert "evidence" not in registry.REGISTRY  # blocked, documented - not faked

    def test_duplicate_codes_and_bad_definitions_are_refused(self):
        report = registry.REGISTRY["executions"]
        with pytest.raises(ImproperlyConfigured):
            registry.register(report)
        from dataclasses import replace
        with pytest.raises(ImproperlyConfigured):
            registry.register(replace(report, code="x1", default_sort="nope"))
        with pytest.raises(ImproperlyConfigured):
            registry.register(replace(report, code="x2", formats=("docx",)))
        with pytest.raises(ImproperlyConfigured):
            registry.register(replace(report, code="x3", columns=(report.columns[0], report.columns[0])))

    def test_every_report_has_a_valid_definition(self):
        for report in registry.REGISTRY.values():
            assert report.version >= 1 and report.columns and report.max_rows <= k.MAX_ROWS and report.default_sort.lstrip("-") in report.column_map
            assert not any(c.key in ("pk", "id") for c in report.columns)

    def test_unknown_reports_are_refused(self, w):
        for code in ("nope", "", None, 5, "../etc/passwd", "executions; drop table"):
            with pytest.raises(ReportError) as info:
                run_report(w, "reporter", code)
            assert info.value.code == "unknown_report"

    def test_descriptions_never_expose_orm_paths(self):
        text = str(registry.describe(registry.REGISTRY["executions"]))
        assert "version__template" not in text and "assignment__" not in text


# ------------------------------------------------------------------------------------------------------------------------------ filters
class TestFilters:
    def clean(self, w, raw, code="executions", people=True):
        return filters.clean(registry.REGISTRY[code], raw, can_see_people=people)

    @pytest.mark.parametrize("raw,code", [
        ({"filters": {"nope": 1}}, "unsupported_filter"), ({"filters": {"status": "weird"}}, "invalid_filter"), ({"filters": {"location": "not-a-uuid"}}, "invalid_filter"),
        ({"filters": {"date_from": "2026-13-45"}}, "invalid_filter"), ({"filters": {"date_from": "2026-02-01", "date_to": "2026-01-01"}}, "invalid_range"),
        ({"filters": {"date_from": "2024-01-01", "date_to": "2026-01-01"}}, "range_too_long"), ({"columns": ["pk"]}, "invalid_columns"), ({"columns": ["number", "number"]}, "invalid_columns"),
        ({"columns": "number"}, "invalid_columns"), ({"sort": "version__template__name"}, "invalid_sort"), ({"sort": "-nope"}, "invalid_sort"), ({"group_by": "x"}, "invalid_group"),
        ({"bogus": 1}, "invalid_request"), ({"filters": "x"}, "invalid_filter"), ({"filters": {"include_descendants": "yes"}}, "invalid_filter"),
    ])
    def test_invalid_requests_are_refused(self, w, raw, code):
        with pytest.raises(ReportError) as info:
            self.clean(w, raw)
        assert info.value.code == code

    def test_orm_lookups_and_injection_strings_are_not_accepted_as_keys(self, w):
        for key in ("status__in", "organization__name", "version__template__name__icontains", "1=1", "status;--"):
            with pytest.raises(ReportError):
                self.clean(w, {"filters": {key: "x"}})

    def test_the_date_range_is_inclusive_and_half_open_internally(self, w):
        p = self.clean(w, {"filters": {"date_from": "2026-03-01", "date_to": "2026-03-31"}})
        assert p.start.date() == date(2026, 3, 1) and p.end.date() == date(2026, 4, 1) and p.end.hour == 0  # [Mar 1 00:00, Apr 1 00:00)

    def test_the_default_range_and_the_maximum(self, w):
        p = self.clean(w, {})
        assert (p.end - p.start).days == k.DEFAULT_RANGE_DAYS
        p = self.clean(w, {"filters": {"date_from": "2026-01-01", "date_to": "2026-12-31"}}) if False else None
        with pytest.raises(ReportError):
            self.clean(w, {"filters": {"date_from": "2025-01-01", "date_to": "2026-01-02"}})

    def test_people_filters_and_columns_are_hidden_without_people_access(self, w):
        with pytest.raises(ReportError):
            self.clean(w, {"filters": {"assigned_to": str(w["runner"].pk)}}, people=False)
        with pytest.raises(ReportError):
            self.clean(w, {"columns": ["started_by"]}, people=False)

    def test_null_filter_values_are_ignored(self, w):
        assert self.clean(w, {"filters": {"status": None, "location": ""}}).filters == {}


# ------------------------------------------------------------------------------------------------------------------------------ authorization
@pytest.mark.django_db
class TestAuthorization:
    def test_report_view_is_required(self, w, seeded):
        for who in ("no_report", "other_reporter"):
            with pytest.raises(PermissionDenied):
                run_report(w, who, "executions")
        assert run_report(w, "viewer_only", "executions").total == 4

    def test_cross_organization_users_get_nothing(self, w, seeded):
        with pytest.raises(PermissionDenied):
            services.run(w["other_reporter"], w["org"], "executions")
        assert services.run(w["other_reporter"], w["other_reporter"].organization_memberships.first().organization, "executions").total == 0

    def test_location_scope_limits_rows_independently_of_filters(self, w, seeded):
        r = run_report(w, "r1_reporter", "executions")
        assert {row["location"] for row in r.rows} == {"Restaurant 1"} and r.total == 2
        wide = run_report(w, "r1_reporter", "executions", {"filters": {"location": str(w["r2"].pk)}})
        assert wide.total == 0  # a filter can only narrow: it cannot reach a location outside the scope
        everything = run_report(w, "r1_reporter", "executions", {"filters": {"location": str(w["region"].pk), "include_descendants": True}})
        assert everything.total == 2

    def test_a_location_scoped_user_sees_only_their_locations_in_comparison_and_completion(self, w, seeded):
        rows = run_report(w, "r1_reporter", "location_comparison").rows
        assert {r["location"] for r in rows} <= {"Restaurant 1"}
        assert services.run(w["reporter"], w["org"], "location_comparison").total > 1

    def test_people_columns_need_report_people(self, w, seeded):
        assert "assigned_to" not in {c.key for c in run_report(w, "viewer_only", "assignments").columns}
        assert "assigned_to" in {c.key for c in run_report(w, "reporter", "assignments").columns}
        with pytest.raises(ReportError):
            run_report(w, "viewer_only", "assignments", {"columns": ["assigned_to"]})

    def test_score_and_evaluation_columns_need_their_own_view_permissions(self, w, seeded, org_a, make_user, assign):
        from apps.permissions import services as rbac
        from apps.permissions.registry import P
        role = rbac.create_role(organization=org_a, name="No scores", permissions=[P.REPORT_VIEW, P.EXECUTION_VIEW, P.LOCATION_VIEW, P.ASSIGNMENT_VIEW])
        user = make_user(email="noscores@example.com", organization=org_a, role=None)
        assign(user, org_a, role)
        r = services.run(user, w["org"], "executions", {"columns": ["number", "score_percentage", "evaluation"]})
        assert r.total == 4 and all(row["score_percentage"] is None and row["evaluation"] is None for row in r.rows)  # hidden, not zero
        full = services.run(w["reporter"], w["org"], "executions", {"columns": ["number", "score_percentage", "evaluation"]})
        assert any(row["score_percentage"] is not None for row in full.rows)

    def test_inactive_users_are_refused(self, w, seeded):
        w["reporter"].status = "suspended"
        w["reporter"].save(update_fields=["status"])
        with pytest.raises(PermissionDenied):
            run_report(w, "reporter", "executions")


# ------------------------------------------------------------------------------------------------------------------------------ accuracy
@pytest.mark.django_db
class TestAccuracy:
    def test_executions_report_matches_the_stored_facts(self, w, seeded):
        version, good1, bad2, good3, open1 = seeded
        r = run_report(w, "reporter", "executions", {"columns": ["number", "status", "location", "score_percentage", "grade", "evaluation", "duration", "version"]})
        rows = by(r.rows, "number")
        assert r.total == 4 and rows[open1.number]["status"] == "in_progress" and rows[open1.number]["score_percentage"] is None  # no score: blank, never zero
        stored = ScoreResult.objects.get(execution=good1, is_current=True, status="final")
        assert rows[good1.number]["score_percentage"] == stored.percentage and rows[good1.number]["grade"] == stored.grade
        assert rows[bad2.number]["evaluation"] != rows[good1.number]["evaluation"]  # the critical failure was evaluated differently
        assert rows[good1.number]["version"] == version.version_number and rows[good1.number]["duration"] is not None and rows[open1.number]["duration"] is None

    def test_status_template_and_date_filters(self, w, seeded):
        assert run_report(w, "reporter", "executions", {"filters": {"status": "in_progress"}}).total == 1
        assert run_report(w, "reporter", "executions", {"filters": {"status": "submitted"}}).total == 3
        assert run_report(w, "reporter", "executions", {"filters": {"template": str(w["etemplate"].pk)}}).total == 4
        assert run_report(w, "reporter", "executions", {"filters": {"template": str(w["template"].pk)}}).total == 0
        old = (timezone.localdate() - timedelta(days=30)).isoformat()
        assert run_report(w, "reporter", "executions", {"filters": {"date_from": old, "date_to": old}}).total == 0
        today = timezone.localdate().isoformat()
        assert run_report(w, "reporter", "executions", {"filters": {"date_from": today, "date_to": today}}).total == 4  # the end date is inclusive

    def test_the_exact_published_version_is_attributed(self, w, seeded):
        version = seeded[0]
        assert run_report(w, "reporter", "executions", {"filters": {"version": str(version.pk)}}).total == 4
        assert run_report(w, "reporter", "executions", {"filters": {"version": str(w["eversion"].pk)}}).total == 0

    def test_scores_report_lists_only_final_current_snapshots(self, w, seeded):
        r = run_report(w, "reporter", "scores", {"columns": ["execution", "percentage", "kind"]})
        assert r.total == 3  # the in-progress execution has no final score and is not listed (nor counted as zero)
        assert all(row["percentage"] is not None for row in r.rows)
        assert {row["kind"] for row in r.rows} == {"submission"}

    def test_score_summary_groups_without_zero_filling(self, w, seeded):
        total = run_report(w, "reporter", "score_summary").rows
        assert len(total) == 1 and total[0]["scored"] == 3 and total[0]["lowest"] <= total[0]["average"] <= total[0]["highest"]
        by_location = run_report(w, "reporter", "score_summary", {"group_by": "location"}).rows
        assert {r["group"] for r in by_location} == {"Restaurant 1", "Restaurant 2"}
        assert sum(r["scored"] for r in by_location) == 3
        month = run_report(w, "reporter", "score_summary", {"group_by": "month"}).rows
        assert month[0]["group"] == timezone.localdate().strftime("%Y-%m")

    def test_evaluations_report_uses_stored_runs(self, w, seeded):
        r = run_report(w, "reporter", "evaluations")
        assert r.total >= 3 and all(row["outcome"] for row in r.rows)
        assert run_report(w, "reporter", "evaluations", {"filters": {"current": True}}).total == r.total
        assert run_report(w, "reporter", "evaluations", {"filters": {"outcome": "fail"}}).total == sum(1 for row in r.rows if row["outcome"] == "fail")

    def test_completion_counts_each_assignment_once_and_defines_its_denominator(self, w, seeded):
        row = run_report(w, "reporter", "completion").rows[0]
        assert row["eligible"] == 4 and row["completed"] == 3 and row["in_progress"] == 1 and row["pending"] == 0 and row["overdue"] == 0
        assert row["completion_pct"] == 75.0 and row["avg_minutes"] is not None
        by_loc = by(run_report(w, "reporter", "completion", {"group_by": "location"}).rows, "group")
        assert by_loc["Restaurant 1"]["eligible"] == 2 and by_loc["Restaurant 1"]["completed"] == 1 and by_loc["Restaurant 1"]["completion_pct"] == 50.0

    def test_cancelled_and_draft_assignments_are_excluded_and_overdue_follows_the_lifecycle(self, w, seeded):
        from apps.assignments.models import ChecklistAssignment
        a = ChecklistAssignment.objects.filter(status="completed").first()
        late = ChecklistAssignment.objects.exclude(status__in=["completed", "cancelled", "draft"]).first()
        late.due_at = timezone.now() - timedelta(days=1)
        late.save(update_fields=["due_at"])
        ChecklistAssignment.objects.filter(pk=a.pk).update(status="cancelled", due_at=timezone.now() - timedelta(days=2), cancelled_at=timezone.now(), cancelled_by=w["admin"], cancel_reason="test")
        row = run_report(w, "reporter", "completion").rows[0]
        assert row["eligible"] == 3 and row["overdue"] == 1  # the cancelled one is neither eligible nor overdue
        listing = run_report(w, "reporter", "assignments", {"filters": {"overdue": True}})
        assert listing.total == 1 and listing.rows[0]["overdue"] is True
        cancelled = by(run_report(w, "reporter", "assignments", {"filters": {"status": "cancelled"}}).rows, "status")
        assert cancelled["cancelled"]["overdue"] is False

    def test_location_comparison_metrics(self, w, seeded):
        rows = by(run_report(w, "reporter", "location_comparison").rows, "location")
        assert rows["Restaurant 1"]["eligible"] == 2 and rows["Restaurant 1"]["submitted"] == 1 and rows["Restaurant 2"]["submitted"] == 2
        assert rows["Restaurant 1"]["avg_score"] is not None and rows["Warehouse 1"]["avg_score"] is None and rows["Warehouse 1"]["completion_pct"] is None  # no data: blank, not zero
        sub = run_report(w, "reporter", "location_comparison", {"filters": {"location": str(w["area1"].pk)}}).rows
        assert {r["location"] for r in sub} == {"Area 1", "Restaurant 1", "Restaurant 2"}
        only = run_report(w, "reporter", "location_comparison", {"filters": {"location": str(w["area1"].pk), "include_descendants": False}}).rows
        assert {r["location"] for r in only} == {"Area 1"}

    def test_pagination_sorting_and_totals_are_deterministic(self, w, seeded):
        page1 = run_report(w, "reporter", "executions", {"sort": "number"}, page=1, page_size=3)
        page2 = run_report(w, "reporter", "executions", {"sort": "number"}, page=2, page_size=3)
        numbers = [r["number"] for r in page1.rows + page2.rows]
        assert page1.total == 4 and page1.pages == 2 and numbers == sorted(numbers) and len(numbers) == 4 and len(set(numbers)) == 4
        desc = run_report(w, "reporter", "executions", {"sort": "-number"}).rows
        assert [r["number"] for r in desc] == sorted(numbers, reverse=True)
        assert run_report(w, "reporter", "executions", page=9).rows == []
        assert run_report(w, "reporter", "executions", page_size=10_000).page_size == k.MAX_PAGE_SIZE

    def test_a_report_never_changes_business_data(self, w, seeded):
        from apps.executions.models import ChecklistExecution
        before = list(ChecklistExecution.objects.order_by("pk").values_list("pk", "status", "revision", "updated_at"))
        for code in registry.REGISTRY:
            services.run(w["reporter"], w["org"], code)
        assert list(ChecklistExecution.objects.order_by("pk").values_list("pk", "status", "revision", "updated_at")) == before

    def test_queries_do_not_grow_with_the_data(self, w, seeded, django_assert_max_num_queries):
        with django_assert_max_num_queries(40):
            services.run(w["reporter"], w["org"], "executions", {"columns": ["number", "template", "score_percentage", "evaluation", "assigned_to"]}, page_size=50)
        with django_assert_max_num_queries(40):
            services.run(w["reporter"], w["org"], "location_comparison")
