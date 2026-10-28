"""Phase 20 web screens, HTMX widgets, charts and API."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse
from rest_framework.test import APIClient

from apps.dashboards import catalog
from apps.dashboards.models import DashboardLayout

from dashboards_support import build_exec_world, seed_executions

API = "/api/v1/dashboards/"


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_exec_world(org_a, org_b, make_user, assign, make_type)


@pytest.fixture
def seeded(w):
    return seed_executions(w)


def web(user):
    c = Client()
    c.force_login(user)
    return c


def api(user=None):
    c = APIClient()
    if user is not None:
        c.force_authenticate(user)
    return c


def body(response):
    data = response.json()
    return data.get("data", data) if isinstance(data, dict) else data


@pytest.mark.django_db
class TestWeb:
    def test_access(self, w, seeded):
        assert Client().get(reverse("dashboards:index")).status_code == 302
        assert web(w["dash_noview"]).get(reverse("dashboards:index")).status_code == 403
        r = web(w["dash"]).get(reverse("dashboards:index"))
        assert r.status_code == 302 and r.url.endswith("/dashboards/overview/")
        assert web(w["dash_findings_only"]).get(reverse("dashboards:dashboard", args=["scores"])).status_code == 404
        assert web(w["dash"]).get(reverse("dashboards:dashboard", args=["nope"])).status_code == 404

    def test_every_dashboard_page_and_widget_renders(self, w, seeded):
        c = web(w["dash"])
        for code, dashboard in catalog.DASHBOARDS.items():
            page = c.get(reverse("dashboards:dashboard", args=[code]))
            assert page.status_code == 200 and dashboard.title in page.content.decode()
            for widget in dashboard.widgets:
                part = c.get(reverse("dashboards:widget", args=[code, widget]))
                assert part.status_code == 200, (code, widget)
                html = part.content.decode()
                assert "Traceback" not in html and "could not be calculated" not in html, (code, widget)

    def test_charts_are_accessible_svg_with_a_data_table(self, w, seeded):
        html = web(w["dash"]).get(reverse("dashboards:widget", args=["operations", "execution_status"])).content.decode()
        assert '<svg class="chart"' in html and 'role="img"' in html and "<desc" in html and "<table" in html
        assert "<script" not in html and 'style="' not in html  # CSP: no inline script or style
        trend = web(w["dash"]).get(reverse("dashboards:widget", args=["operations", "execution_trend"])).content.decode()
        assert "<polyline" in trend or "<circle" in trend

    def test_invalid_filters_show_a_recoverable_message(self, w, seeded):
        page = web(w["dash"]).get(reverse("dashboards:dashboard", args=["overview"]) + "?date_from=2026-02-01&date_to=2026-01-01")
        assert page.status_code == 200 and "Reset the filters" in page.content.decode() and "/dashboards/overview/widgets/" not in page.content.decode()
        part = web(w["dash"]).get(reverse("dashboards:widget", args=["overview", "kpi_completion_rate"]) + "?location=bad")
        assert part.status_code == 200 and "not a valid identifier" in part.content.decode()

    def test_widgets_outside_the_dashboard_or_permission_are_refused(self, w, seeded):
        c = web(w["dash"])
        assert c.get(reverse("dashboards:widget", args=["findings", "kpi_average_score"])).status_code == 404  # not on that dashboard
        assert web(w["dash_findings_only"]).get(reverse("dashboards:widget", args=["overview", "kpi_average_score"])).status_code == 403

    def test_labels_are_escaped(self, w, seeded):
        from apps.locations.models import Location
        Location.objects.filter(pk=w["r1"].pk).update(name='<img src=x onerror="alert(1)">')
        html = web(w["dash"]).get(reverse("dashboards:widget", args=["locations", "location_comparison"])).content.decode()
        assert "<img src=x" not in html and "&lt;img" in html

    def test_customize_saves_reorders_hides_and_resets_with_csrf(self, w, seeded):
        dashboard = catalog.DASHBOARDS["overview"]
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(w["dash"])
        assert strict.post(reverse("dashboards:customize", args=["overview"]), {"action": "save"}).status_code == 403
        c = web(w["dash"])
        form = c.get(reverse("dashboards:customize", args=["overview"]))
        assert form.status_code == 200 and 'aria-label="Show Completion rate"' in form.content.decode()  # each show/hide control is named
        post = {"action": "save", "widget": list(dashboard.widgets)}
        for i, code in enumerate(dashboard.widgets):
            post[f"position_{code}"] = str(len(dashboard.widgets) - i)
            if i:
                post[f"show_{code}"] = "1"
        c.post(reverse("dashboards:customize", args=["overview"]), post)
        layout = DashboardLayout.objects.get(user=w["dash"])
        assert layout.widgets[0]["code"] == dashboard.widgets[-1] and layout.widgets[-1] == {"code": dashboard.widgets[0], "hidden": True}
        page = c.get(reverse("dashboards:dashboard", args=["overview"])).content.decode()
        assert reverse("dashboards:widget", args=["overview", dashboard.widgets[0]]) not in page
        c.post(reverse("dashboards:customize", args=["overview"]), {"action": "save", "widget": ["nope"]})
        assert DashboardLayout.objects.get(user=w["dash"]).revision == 1  # invalid layout rejected
        c.post(reverse("dashboards:customize", args=["overview"]), {"action": "save_shared", "widget": []})
        assert not DashboardLayout.objects.filter(user__isnull=True).exists()  # needs dashboard.manage
        c.post(reverse("dashboards:customize", args=["overview"]), {"action": "reset"})
        assert not DashboardLayout.objects.filter(user=w["dash"]).exists()

    def test_drill_down_destination_reauthorizes(self, w, seeded):
        html = web(w["dash"]).get(reverse("dashboards:widget", args=["overview", "kpi_assignments_overdue"])).content.decode()
        assert "/reports/assignments/?" in html and "f_overdue=1" in html
        assert web(w["dash_noview"]).get(reverse("reporting:report", args=["assignments"]) + "?f_overdue=1").status_code == 200  # has report.view: allowed by the report itself
        assert web(w["dash_findings_only"]).get(reverse("reporting:report", args=["assignments"])).status_code in (200, 403)


@pytest.mark.django_db
class TestApi:
    def test_listing_and_catalogue(self, w, seeded):
        assert api().get(API).status_code in (401, 403)
        assert api(w["dash_noview"]).get(API).status_code == 403
        listing = body(api(w["dash"]).get(API))
        assert {d["code"] for d in listing["dashboards"]} >= {"overview", "scores"} and listing["can_manage"] is False
        k = body(api(w["dash_findings_only"]).get(API + "kpis/"))
        assert {x["code"] for x in k["kpis"]} <= {"findings_created", "findings_open", "findings_top_severity"} and k["kpis"]

    def test_dashboard_data_is_calculated_and_strict(self, w, seeded):
        c = api(w["dash"])
        r = c.post(API + "operations/data/", {"filters": {}, "widgets": ["kpi_assignments_eligible", "execution_status"]}, format="json")
        d = body(r)
        assert r.status_code == 200 and d["widgets"][0]["value"] == 4 and d["widgets"][1]["series"] and d["period"]["to"]
        assert c.post(API + "operations/data/", {"widgets": ["kpi_average_score_x"]}, format="json").status_code == 400
        assert c.post(API + "operations/data/", {"filters": {"nope": 1}}, format="json").status_code == 400
        assert c.post(API + "operations/data/", {"bogus": 1}, format="json").status_code == 400
        assert c.post(API + "nope/data/", {}, format="json").status_code == 404
        full = body(c.post(API + "overview/data/", {}, format="json"))
        assert {x["code"] for x in full["widgets"]} == set(catalog.DASHBOARDS["overview"].widgets)

    def test_widget_endpoint_and_scope(self, w, seeded):
        assert body(api(w["dash_r1"]).get(API + "widgets/kpi_assignments_eligible/data/"))["value"] == 2
        assert body(api(w["dash"]).get(API + "widgets/kpi_assignments_eligible/data/?location=" + str(w["r2"].pk)))["value"] == 2
        assert api(w["dash_findings_only"]).get(API + "widgets/kpi_average_score/data/").status_code == 403
        assert api(w["dash"]).get(API + "widgets/nope/data/").status_code == 404
        assert api(w["dash"]).get(API + "widgets/kpi_assignments_eligible/data/?date_from=bad").status_code == 400

    def test_layout_endpoints(self, w, seeded):
        c = api(w["dash"])
        first = catalog.DASHBOARDS["overview"].widgets[0]
        assert body(c.put(API + "overview/layout/", {"widgets": [{"code": first, "hidden": True}]}, format="json"))["layout_source"] == "personal"
        assert c.put(API + "overview/layout/", {"widgets": [{"code": "evil"}]}, format="json").status_code == 400
        assert c.put(API + "overview/layout/", {"widgets": [{"code": first, "script": "x"}]}, format="json").status_code == 400
        assert c.put(API + "overview/layout/shared/", {"widgets": []}, format="json").status_code == 403
        assert body(api(w["dash_manager"]).put(API + "overview/layout/shared/", {"widgets": []}, format="json"))
        assert body(c.delete(API + "overview/layout/"))["layout_source"] == "organization"


@pytest.mark.django_db
def test_admin_is_read_only(admin_client):
    assert admin_client.get("/admin/dashboards/dashboardlayout/").status_code == 200
    assert admin_client.get("/admin/dashboards/dashboardlayout/add/").status_code == 403
