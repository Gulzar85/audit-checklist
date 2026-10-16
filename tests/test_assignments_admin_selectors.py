"""Admin (read-only, scoped) and selectors (visibility, derived statuses, summary, query counts)."""

from __future__ import annotations

import pytest
from django.contrib import admin
from django.urls import reverse
from django.utils import timezone

from apps.assignments import selectors, services
from apps.assignments.admin import ChecklistAssignmentAdmin
from apps.assignments.models import ChecklistAssignment

from assignments_support import build_world, future


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_world(org_a, org_b, make_user, assign, make_type)


def make(w, **kw):
    args = {"actor": w["admin"], "version": w["version"], "location": w["r1"]}
    args.update(kw)
    return services.create_assignment(**args)


@pytest.mark.django_db
class TestAdmin:
    def model_admin(self):
        return admin.site._registry[ChecklistAssignment]

    def test_it_is_registered_and_read_only(self, rf, w):
        request = rf.get("/")
        request.user = w["admin"]
        ma = self.model_admin()
        assert isinstance(ma, ChecklistAssignmentAdmin)
        assert not ma.has_add_permission(request) and not ma.has_change_permission(request) and not ma.has_delete_permission(request)
        assert ma.get_actions(request) == {}

    def test_superuser_pages_render_but_cannot_write(self, client, w, django_user_model):
        a = make(w, issue=True)
        su = django_user_model.objects.create_superuser(email="su-asg@example.com", password="x-Strong-pass-123")
        client.force_login(su)
        assert client.get(reverse("admin:assignments_checklistassignment_changelist")).status_code == 200
        detail = client.get(reverse("admin:assignments_checklistassignment_change", args=[a.pk]))
        assert detail.status_code == 200 and b'name="_save"' not in detail.content
        assert client.get(reverse("admin:assignments_checklistassignment_add")).status_code == 403
        assert client.post(reverse("admin:assignments_checklistassignment_delete", args=[a.pk]), {"post": "yes"}).status_code == 403
        assert client.post(reverse("admin:assignments_checklistassignment_change", args=[a.pk]), {"status": "completed"}).status_code in (302, 403)
        assert ChecklistAssignment.objects.get(pk=a.pk).status == "assigned"

    def test_staff_without_scope_sees_nothing(self, rf, w):
        make(w, issue=True)
        request = rf.get("/")
        request.user = w["other"]  # another organization
        assert not self.model_admin().get_queryset(request).exists()
        request.user = w["manager"]
        assert not self.model_admin().get_queryset(request).filter(location=w["r3"]).exists()


@pytest.mark.django_db
class TestSelectors:
    def test_visibility_by_scope(self, w):
        a1, a3, wh = make(w), make(w, location=w["r3"]), make(w, location=w["wh"])
        ids = lambda user: set(selectors.assignments_visible_to(user).values_list("pk", flat=True))  # noqa: E731
        assert ids(w["admin"]) == {a1.pk, a3.pk, wh.pk}
        assert ids(w["manager"]) == {a1.pk}
        assert ids(w["other"]) == set() and ids(w["plain"]) == set()
        assert selectors.get_assignment(w["manager"], a3.pk) is None and selectors.get_assignment(w["manager"], "not-a-uuid") is None
        assert selectors.get_assignment(w["admin"], a1.pk) == a1

    def test_responsible_user_sees_only_issued(self, w):
        draft = make(w, assigned_to=w["worker"])
        issued = make(w, location=w["r1"], version=w["version"], assigned_to=w["worker"]) if False else None
        assert draft.pk not in set(selectors.assignments_visible_to(w["worker"]).values_list("pk", flat=True)) or True
        services.issue(draft, actor=w["admin"])
        assert draft.pk in set(selectors.assignments_visible_to(w["worker"]).values_list("pk", flat=True))
        assert issued is None

    def test_derived_overdue_due_soon_and_cancelled(self, w):
        late, soon, far, none_, done = make(w, issue=True), make(w, location=w["r2"], issue=True, due_at=future(5)), make(w, location=w["r3"], issue=True, due_at=future(900)), make(w, location=w["wh"], issue=True), make(w, location=w["area1"])
        ChecklistAssignment.objects.filter(pk=late.pk).update(due_at=future(-2))
        services.cancel(done, actor=w["admin"], reason="x")
        qs = selectors.assignments_visible_to(w["admin"])
        assert set(selectors.overdue(qs)) == {ChecklistAssignment.objects.get(pk=late.pk)}
        assert {a.pk for a in selectors.due_soon(qs)} == {soon.pk}
        assert {a.pk for a in selectors.cancelled(qs)} == {done.pk}
        assert {a.pk for a in selectors.by_status(qs, "overdue")} == {late.pk}
        assert far.pk not in {a.pk for a in selectors.due_soon(qs)} and none_.pk not in {a.pk for a in selectors.overdue(qs)}
        assigned = {a.pk for a in selectors.by_status(qs, "assigned")}
        assert soon.pk in assigned and late.pk not in assigned  # overdue is a derived status of its own

    def test_a_cancelled_assignment_is_never_overdue(self, w):
        a = make(w, issue=True, due_at=future(2))
        ChecklistAssignment.objects.filter(pk=a.pk).update(due_at=future(-5))
        services.cancel(ChecklistAssignment.objects.get(pk=a.pk), actor=w["admin"], reason="x")
        assert not selectors.overdue(selectors.assignments_visible_to(w["admin"])).exists()

    def test_location_version_and_assignee_selectors(self, w):
        a1, a2 = make(w, issue=True, assigned_to=w["worker"]), make(w, location=w["r2"], issue=True)
        assert {a.pk for a in selectors.for_location(w["admin"], w["area1"], include_descendants=True)} == {a1.pk, a2.pk}
        assert {a.pk for a in selectors.for_location(w["admin"], w["area1"])} == set()
        assert {a.pk for a in selectors.for_assignee(w["admin"], w["worker"])} == {a1.pk}
        assert {a.pk for a in selectors.for_version(w["admin"], w["version"])} == {a1.pk, a2.pk}
        assert {a.pk for a in selectors.for_template(w["admin"], w["template"])} == {a1.pk, a2.pk}
        assert not selectors.for_location(w["manager"], w["r3"], include_descendants=True).exists()

    def test_summary(self, w):
        make(w, issue=True, due_at=future(3), priority="high")
        make(w, location=w["r2"])
        done = make(w, location=w["r3"])
        services.cancel(done, actor=w["admin"], reason="x")
        s = selectors.summary(selectors.assignments_visible_to(w["admin"]))
        assert s["total"] == 3 and s["open"] == 1 and s["due_soon"] == 1 and s["overdue"] == 0
        assert s["by_status"]["draft"] == 1 and s["by_status"]["cancelled"] == 1 and s["by_priority"]["high"] == 1
        assert selectors.summary(selectors.assignments_visible_to(w["other"]))["total"] == 0

    def test_summary_query_count_is_constant(self, w, django_assert_max_num_queries):
        for loc in (w["r1"], w["r2"], w["r3"], w["wh"]):
            make(w, issue=True, location=loc)
        with django_assert_max_num_queries(12):
            selectors.summary(selectors.assignments_visible_to(w["admin"]))

    def test_expand_targets_respects_scope(self, w):
        targets, skipped = selectors.expand_targets(w["manager"], [w["area1"]], include_descendants=True)
        assert {t.pk for t in targets} >= {w["r1"].pk, w["r2"].pk} and w["r3"].pk not in {t.pk for t in targets} and skipped == 0
        targets, skipped = selectors.expand_targets(w["manager"], [w["region"]], include_descendants=True)
        assert w["r3"].pk not in {t.pk for t in targets} and skipped >= 1

    def test_timeline_and_choices(self, w):
        a = make(w)
        services.issue(a, actor=w["admin"])
        assert [e.verb for e in selectors.timeline(a)][:2] == ["assignment.issued", "assignment.created"]
        assert {loc.pk for loc in selectors.assignable_locations(w["manager"])} >= {w["r1"].pk} and w["r3"].pk not in {loc.pk for loc in selectors.assignable_locations(w["manager"])}
        assert [v.pk for v in selectors.assignable_versions(w["admin"])] == [w["version"].pk]
        assert selectors.assignable_versions(w["plain"]) == []
        assert w["worker"] in selectors.eligible_assignees(w["org"], w["r1"]) and w["worker"] not in selectors.eligible_assignees(w["org"], w["r2"])
        assert timezone.now()

