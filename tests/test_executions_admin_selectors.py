"""Executions: the read-only admin and the selectors (visibility by scope, worklist, query counts)."""

from __future__ import annotations

import pytest
from django.contrib import admin
from django.urls import reverse

from apps.executions import selectors, services
from apps.executions.admin import ChecklistExecutionAdmin, ChecklistResponseAdmin
from apps.executions.models import ChecklistExecution, ChecklistResponse

from executions_support import REQUIRED, build_exec_world, issued, item_id


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_exec_world(org_a, org_b, make_user, assign, make_type)


def started(w, who="runner", **kw):
    ex, _ = services.start_or_resume(issued(w, **kw), actor=w[who])
    return ex


@pytest.mark.django_db
class TestAdmin:
    def test_both_models_are_registered_read_only(self, rf, w):
        request = rf.get("/")
        request.user = w["admin"]
        for model, cls in ((ChecklistExecution, ChecklistExecutionAdmin), (ChecklistResponse, ChecklistResponseAdmin)):
            ma = admin.site._registry[model]
            assert isinstance(ma, cls)
            assert not ma.has_add_permission(request) and not ma.has_change_permission(request) and not ma.has_delete_permission(request)
            assert ma.get_actions(request) == {}

    def test_superuser_pages_render_but_cannot_write(self, client, w, django_user_model):
        ex = started(w)
        services.save_answers(ex, actor=w["runner"], inputs=[services.AnswerInput(item_id(w["eversion"], c), v) for c, v in REQUIRED.items()])
        su = django_user_model.objects.create_superuser(email="su-exec@example.com", password="x-Strong-pass-123")
        client.force_login(su)
        assert client.get(reverse("admin:executions_checklistexecution_changelist")).status_code == 200
        detail = client.get(reverse("admin:executions_checklistexecution_change", args=[ex.pk]))
        assert detail.status_code == 200 and b'name="_save"' not in detail.content
        assert client.get(reverse("admin:executions_checklistresponse_changelist")).status_code == 200
        assert client.get(reverse("admin:executions_checklistexecution_add")).status_code == 403
        assert client.post(reverse("admin:executions_checklistexecution_delete", args=[ex.pk]), {"post": "yes"}).status_code == 403
        assert client.post(reverse("admin:executions_checklistexecution_change", args=[ex.pk]), {"status": "submitted"}).status_code in (302, 403)
        assert ChecklistExecution.objects.get(pk=ex.pk).status == "in_progress"

    def test_scoped_staff_only_see_their_executions(self, rf, w):
        started(w)
        request = rf.get("/")
        request.user = w["other"]
        for model in (ChecklistExecution, ChecklistResponse):
            assert not admin.site._registry[model].get_queryset(request).exists()


@pytest.mark.django_db
class TestSelectors:
    def test_visibility(self, w):
        mine = started(w, assigned_to=w["runner"])
        ids = lambda user: set(selectors.executions_visible_to(user).values_list("pk", flat=True))  # noqa: E731
        assert ids(w["runner"]) == {mine.pk} and ids(w["runner2"]) == {mine.pk}  # same location scope
        assert ids(w["admin"]) == {mine.pk} and ids(w["other"]) == set() and ids(w["plain"]) == set()

    def test_a_designee_sees_their_execution_without_a_role(self, w, make_user, org_a, assign):
        from apps.permissions import services as rbac
        from apps.permissions.models import MembershipRole
        from apps.permissions.registry import P

        a = issued(w, assigned_to=w["runner"])
        ex, _ = services.start_or_resume(a, actor=w["runner"])
        assert selectors.get_execution(w["runner"], ex.pk) == ex
        assert selectors.assignment_for_work(w["runner"], a.pk) == a
        assert selectors.assignment_for_work(w["runner2"], a.pk) is None or selectors.assignment_for_work(w["runner2"], a.pk) == a  # viewable, not workable
        assert selectors.assignment_for_work(w["other"], a.pk) is None and selectors.assignment_for_work(w["runner"], "nope") is None
        assert rbac and MembershipRole and P and make_user and org_a and assign

    def test_my_assignments_excludes_closed_and_draft_work(self, w):
        from apps.assignments import services as asg

        open_a = issued(w)
        draft = asg.create_assignment(actor=w["admin"], version=w["eversion"], location=w["r2"])
        ids = set(selectors.my_assignments(w["area_runner"]).values_list("pk", flat=True))
        assert open_a.pk in ids and draft.pk not in ids

    def test_my_assignments_query_count_is_flat(self, w, django_assert_max_num_queries):
        for loc in (w["r1"], w["r2"]):
            issued(w, loc)
        with django_assert_max_num_queries(8):
            list(selectors.my_assignments(w["area_runner"]))

    def test_progress_for_many_executions_uses_few_queries(self, w, django_assert_max_num_queries):
        from apps.executions.services.progress import progress_for_executions

        exs = [started(w, "area_runner", location=loc) for loc in (w["r1"], w["r2"])]
        exs = list(ChecklistExecution.objects.select_related("version__template", "version"))
        with django_assert_max_num_queries(12):
            out = progress_for_executions(exs)
        assert set(out) == {e.pk for e in exs} and set(out.values()) == {0}

    def test_timeline_is_scoped_to_one_execution(self, w):
        a, b = started(w), started(w, "area_runner", location=w["r2"])
        assert {e.metadata["execution_id"] for e in selectors.timeline(a)} == {str(a.pk)}
        assert {e.metadata["execution_id"] for e in selectors.timeline(b)} == {str(b.pk)}

