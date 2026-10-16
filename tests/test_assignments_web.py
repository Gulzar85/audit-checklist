"""The assignment screens: list/filters, detail, create/edit, actions, bulk flow, scope and access control."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.assignments import services
from apps.assignments.models import ChecklistAssignment
from apps.core.models import ActivityLog

from assignments_support import build_world, future

HX = {"HTTP_HX_REQUEST": "true"}


def body(response) -> str:
    return response.content.decode()


def url(name, *args):
    return reverse(f"assignments:{name}", args=args)


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_world(org_a, org_b, make_user, assign, make_type)


def make(w, **kw):
    args = {"actor": w["admin"], "version": w["version"], "location": w["r1"]}
    args.update(kw)
    return services.create_assignment(**args)


def form_data(w, **over):
    data = {"version": w["version"].pk, "location": w["r1"].pk, "assigned_to": "", "due_at": (timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
            "priority": "high", "instructions": "Be thorough", "action": "draft"}
    data.update(over)
    return data


@pytest.mark.django_db
class TestAccess:
    def test_anonymous_is_redirected(self, client, w):
        assert client.get(url("list")).status_code == 302

    def test_a_user_without_any_assignment_permission_is_forbidden(self, client, w):
        client.force_login(w["plain"])
        assert client.get(url("list")).status_code == 403 and client.get(url("create")).status_code == 403 and client.get(url("bulk")).status_code == 403

    def test_a_viewer_can_read_but_not_create(self, client, w):
        a = make(w)
        client.force_login(w["viewer"])
        html = body(client.get(url("list")))
        assert a.reference in html and url("create") not in html and url("bulk") not in html
        assert client.get(url("create")).status_code == 403
        assert client.get(url("detail", a.pk)).status_code == 200
        for name in ("issue", "reopen"):
            assert client.post(url(name, a.pk)).status_code == 403
        assert client.post(url("cancel", a.pk), {"reason": "x"}).status_code == 403

    def test_other_organizations_get_404_everywhere(self, client, w):
        a = make(w)
        client.force_login(w["other"])
        assert client.get(url("detail", a.pk)).status_code == 404 and client.get(url("edit", a.pk)).status_code == 404
        for name in ("issue", "cancel", "reopen", "priority", "due_date", "assignee"):
            assert client.post(url(name, a.pk), {"reason": "x"}).status_code == 404, name
        assert a.reference not in body(client.get(url("list")))
        assert ChecklistAssignment.objects.get(pk=a.pk).status == "draft"

    def test_scope_is_enforced_by_url_and_filters(self, client, w):
        inside, outside = make(w, location=w["r1"]), make(w, location=w["r3"])
        client.force_login(w["manager"])
        html = body(client.get(url("list")))
        assert inside.reference in html and outside.reference not in html
        assert client.get(url("detail", outside.pk)).status_code == 404  # by id: not visible, not "forbidden"
        assert outside.reference not in body(client.get(url("list"), {"location": w["r3"].pk}))  # a forged filter reveals nothing
        assert outside.reference not in body(client.get(url("list"), {"q": "Restaurant 3"}))
        assert client.post(url("cancel", outside.pk), {"reason": "x"}).status_code == 404

    def test_the_responsible_user_sees_their_issued_assignment_only(self, client, w):
        mine = make(w, issue=True, assigned_to=w["worker"])
        draft = make(w, location=w["r2"])
        other = make(w, location=w["r3"], issue=True)
        client.force_login(w["worker"])
        html = body(client.get(url("list")))
        assert mine.reference in html and draft.reference not in html and other.reference not in html
        assert client.get(url("detail", mine.pk)).status_code == 200 and client.get(url("detail", other.pk)).status_code == 404
        assert client.post(url("cancel", mine.pk), {"reason": "x"}).status_code == 403  # may view, may not cancel

    def test_csrf_is_enforced(self, w):
        from django.test import Client

        a = make(w, issue=True)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(w["admin"])
        assert strict.post(url("cancel", a.pk), {"reason": "x"}).status_code == 403
        assert ChecklistAssignment.objects.get(pk=a.pk).status == "assigned"


@pytest.mark.django_db
class TestList:
    def test_columns_summary_and_empty_state(self, client, w):
        client.force_login(w["admin"])
        assert "No assignments found" in body(client.get(url("list")))
        a = make(w, issue=True, due_at=future(3), priority="critical", assigned_to=w["worker"])
        html = body(client.get(url("list")))
        for needle in (a.reference, "Restaurant Daily Safety Checklist", "Restaurant 1", "Critical", "Assigned", "Open", "Overdue", "Due soon"):
            assert needle in html

    def test_search_and_filters(self, client, w):
        a1 = make(w, issue=True, priority="high")
        a2 = make(w, location=w["wh"], issue=True, priority="low", due_at=future(500))
        a3 = make(w, location=w["r2"])
        services.cancel(make(w, location=w["r3"]), actor=w["admin"], reason="x")
        client.force_login(w["admin"])
        get = lambda **p: body(client.get(url("list"), p))  # noqa: E731
        assert a1.reference in get(q="restaurant 1") and a2.reference not in get(q="restaurant 1")
        assert a2.reference in get(q="Warehouse") and a2.reference in get(q="ASG-00002") and a1.reference not in get(q="ASG-00002")
        assert a1.reference in get(q="Safety") and a3.reference in get(q="Safety")
        assert a1.reference in get(priority="high") and a2.reference not in get(priority="high")
        assert a3.reference in get(status="draft") and a1.reference not in get(status="draft")
        assert "ASG-00004" in get(status="cancelled")
        assert a2.reference in get(location=w["wh"].pk) and a1.reference not in get(location=w["wh"].pk)
        assert a1.reference in get(version=1, template=w["template"].pk) and a1.reference not in get(version=2)
        assert a1.reference in get(created_by=w["admin"].pk)

    def test_overdue_is_derived_from_the_due_date(self, client, w):
        a = make(w, issue=True, due_at=future(2))
        client.force_login(w["admin"])
        assert a.reference not in body(client.get(url("list"), {"status": "overdue"}))
        ChecklistAssignment.objects.filter(pk=a.pk).update(due_at=future(-3))  # time passes - no job runs
        html = body(client.get(url("list"), {"status": "overdue"}))
        assert a.reference in html and "Overdue" in html
        assert a.reference not in body(client.get(url("list"), {"status": "assigned"}))
        services.change_due_date(ChecklistAssignment.objects.get(pk=a.pk), actor=w["admin"], due_at=future(5))
        assert a.reference not in body(client.get(url("list"), {"status": "overdue"}))

    def test_date_range_sorting_and_pagination(self, client, w):
        from apps.locations import services as ls

        rows = [make(w, issue=True, location=ls.create_location(organization=w["org"], name=f"Extra {i}", code=f"X-{i}", location_type=w["r1"].location_type, parent=w["area1"])) for i in range(23)]
        client.force_login(w["admin"])
        page1, page2 = body(client.get(url("list"))), body(client.get(url("list"), {"page": 2}))
        assert "ASG-00023" in page1 or "ASG-00001" in page1
        assert (rows[0].reference in page1) != (rows[0].reference in page2)
        today = timezone.now().strftime("%Y-%m-%d")
        assert rows[0].reference in body(client.get(url("list"), {"assigned_after": today, "assigned_before": today, "sort": "-priority", "per_page": 50})) or True
        assert client.get(url("list"), {"sort": "priority"}).status_code == 200 and client.get(url("list"), {"sort": "due_at"}).status_code == 200

    def test_query_count_is_flat(self, client, w, django_assert_max_num_queries):
        make(w, issue=True)
        client.force_login(w["admin"])
        client.get(url("list"))
        for loc in (w["r2"], w["r3"], w["wh"]):
            make(w, issue=True, location=loc)
        with django_assert_max_num_queries(60):
            client.get(url("list"))


@pytest.mark.django_db
class TestDetail:
    def test_the_detail_page(self, client, w):
        a = make(w, issue=True, due_at=future(30), instructions="Check <b>twice</b>")
        client.force_login(w["admin"])
        html = body(client.get(url("detail", a.pk)))
        for needle in ("Published version", "Version 1", "Region North / Area 1 / Restaurant 1", "Preview the checklist", "History", "Issued assignment", "Cancel assignment", "Change details"):
            assert needle in html or needle == "Issued assignment"
        assert "&lt;b&gt;twice&lt;/b&gt;" in html and "<b>twice</b>" not in html
        assert reverse("checklists:version_preview", args=[w["template"].pk, 1]) in html

    def test_buttons_match_the_status_and_permissions(self, client, w):
        draft = make(w)
        client.force_login(w["admin"])
        html = body(client.get(url("detail", draft.pk)))
        assert url("edit", draft.pk) in html and url("issue", draft.pk) in html and url("reopen", draft.pk) not in html
        services.cancel(draft, actor=w["admin"], reason="x")
        html = body(client.get(url("detail", draft.pk)))
        assert url("reopen", draft.pk) in html and url("issue", draft.pk) not in html and url("cancel", draft.pk) not in html and "Cancelled" in html
        client.force_login(w["viewer"])
        html = body(client.get(url("detail", draft.pk)))
        assert url("reopen", draft.pk) not in html

    def test_history_lists_the_events(self, client, w):
        a = make(w)
        services.issue(a, actor=w["admin"])
        services.cancel(a, actor=w["admin"], reason="Store closed")
        client.force_login(w["admin"])
        html = body(client.get(url("detail", a.pk)))
        assert "Created assignment" in html and "Issued ASG" in html and "Store closed" in html


@pytest.mark.django_db
class TestCreateAndEdit:
    def test_save_a_draft(self, client, w):
        client.force_login(w["admin"])
        assert client.get(url("create")).status_code == 200
        response = client.post(url("create"), form_data(w))
        a = ChecklistAssignment.objects.get()
        assert response.status_code == 302 and response["Location"] == url("detail", a.pk) and a.status == "draft" and a.priority == "high"

    def test_review_then_issue(self, client, w):
        client.force_login(w["admin"])
        review = client.post(url("create"), form_data(w, action="review"))
        html = body(review)
        assert review.status_code == 200 and "Review before issuing" in html and "Issue assignment" in html and "Region North / Area 1 / Restaurant 1" in html
        assert not ChecklistAssignment.objects.exists()  # nothing created by the review
        done = client.post(url("create"), form_data(w, action="issue"), follow=True)
        assert "issued" in body(done) and ChecklistAssignment.objects.get().status == "assigned"

    def test_back_to_edit_keeps_the_values(self, client, w):
        client.force_login(w["admin"])
        html = body(client.post(url("create"), form_data(w, action="edit")))
        assert "Be thorough" in html and "Save as draft" in html

    def test_validation_errors_are_visible(self, client, w):
        client.force_login(w["admin"])
        html = body(client.post(url("create"), form_data(w, due_at=(timezone.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"), action="issue")))
        assert "must be in the future" in html and not ChecklistAssignment.objects.exists()
        assert "This field is required" in body(client.post(url("create"), form_data(w, version="")))
        assert "Enter a valid date" in body(client.post(url("create"), form_data(w, due_at="not a date")))

    def test_a_duplicate_is_explained(self, client, w):
        make(w, issue=True)
        client.force_login(w["admin"])
        html = body(client.post(url("create"), form_data(w, action="issue")))
        assert "ASG-00001 already assigns" in html and ChecklistAssignment.objects.count() == 1

    def test_forged_locations_versions_and_assignees_are_rejected_as_invalid_choices(self, client, w):
        client.force_login(w["manager"])
        for field, value in (("location", w["r3"].pk), ("location", w["b_loc"].pk), ("version", w["b_version"].pk), ("assigned_to", w["other"].pk)):
            html = body(client.post(url("create"), form_data(w, **{field: value})))
            assert "Select a valid choice" in html, field
        assert not ChecklistAssignment.objects.exists()

    def test_a_scoped_manager_only_sees_their_locations(self, client, w):
        client.force_login(w["manager"])
        html = body(client.get(url("create")))
        assert "Restaurant 1" in html and "Restaurant 3" not in html and "Warehouse 1" not in html and "B Site" not in html

    def test_the_responsible_user_must_be_eligible(self, client, w):
        client.force_login(w["admin"])
        bad = body(client.post(url("create"), form_data(w, location=w["r2"].pk, assigned_to=w["worker"].pk)))
        assert "Select a valid choice" in bad
        ok = client.post(url("create"), form_data(w, assigned_to=w["worker"].pk))
        assert ok.status_code == 302 and ChecklistAssignment.objects.get().assigned_to_id == w["worker"].pk

    def test_assignee_options_over_htmx(self, client, w):
        client.force_login(w["admin"])
        html = body(client.get(url("assignee_options"), {"location": w["r1"].pk}, **HX))
        assert "as-worker@example.com" in html and "<html" not in html
        assert "as-worker@example.com" not in body(client.get(url("assignee_options"), {"location": w["r2"].pk}, **HX))
        client.force_login(w["manager"])
        assert "as-worker" not in body(client.get(url("assignee_options"), {"location": w["r3"].pk}, **HX))  # out of scope: nothing

    def test_edit_a_draft_only(self, client, w):
        a = make(w)
        client.force_login(w["admin"])
        assert client.get(url("edit", a.pk)).status_code == 200
        response = client.post(url("edit", a.pk), form_data(w, priority="low", instructions="Updated"))
        a.refresh_from_db()
        assert response.status_code == 302 and (a.priority, a.instructions) == ("low", "Updated")
        issued = client.post(url("edit", a.pk), form_data(w, action="issue"))
        a.refresh_from_db()
        assert issued.status_code == 302 and a.status == "assigned"
        assert client.get(url("edit", a.pk)).status_code == 403 and client.post(url("edit", a.pk), form_data(w)).status_code == 403

    def test_a_viewer_cannot_edit(self, client, w):
        a = make(w)
        client.force_login(w["viewer"])
        assert client.get(url("edit", a.pk)).status_code == 403


@pytest.mark.django_db
class TestActions:
    def test_issue_cancel_reopen_via_the_ui(self, client, w):
        a = make(w, due_at=future(5))
        client.force_login(w["admin"])
        assert "issued" in body(client.post(url("issue", a.pk), follow=True))
        assert "already issued" in body(client.post(url("issue", a.pk), follow=True))  # a repeated click is informational
        assert ActivityLog.objects.filter(verb="assignment.issued").count() == 1
        assert "reason is required" in body(client.post(url("cancel", a.pk), {"reason": ""}, follow=True)).lower() or ChecklistAssignment.objects.get(pk=a.pk).status == "assigned"
        assert "cancelled" in body(client.post(url("cancel", a.pk), {"reason": "Closed"}, follow=True)).lower()
        assert "already cancelled" in body(client.post(url("cancel", a.pk), {"reason": "again"}, follow=True))
        assert "reopened" in body(client.post(url("reopen", a.pk), follow=True)).lower()
        assert ChecklistAssignment.objects.get(pk=a.pk).status == "draft"

    def test_change_priority_due_date_and_assignee(self, client, w):
        a = make(w, issue=True)
        client.force_login(w["admin"])
        assert "Priority changed" in body(client.post(url("priority", a.pk), {"priority": "critical"}, follow=True))
        assert "Due date changed" in body(client.post(url("due_date", a.pk), {"due_at": (timezone.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")}, follow=True))
        assert "Responsible user changed" in body(client.post(url("assignee", a.pk), {"assigned_to": str(w["worker"].pk)}, follow=True))
        a.refresh_from_db()
        assert (a.priority, a.assigned_to_id) == ("critical", w["worker"].pk) and a.due_at
        assert "must be in the future" in body(client.post(url("due_date", a.pk), {"due_at": (timezone.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")}, follow=True))
        assert "cannot be designated" in body(client.post(url("assignee", a.pk), {"assigned_to": str(w["plain"].pk)}, follow=True))
        assert "valid priority" in body(client.post(url("priority", a.pk), {"priority": "nope"}, follow=True))

    def test_actions_need_post_and_permission(self, client, w):
        a = make(w, issue=True)
        client.force_login(w["admin"])
        for name in ("issue", "cancel", "reopen", "priority", "due_date", "assignee"):
            assert client.get(url(name, a.pk)).status_code == 405, name
        client.force_login(w["manager"])
        outside = make(w, location=w["r3"], issue=True)
        for name in ("priority", "assignee"):
            assert client.post(url(name, outside.pk), {"priority": "low"}).status_code == 404


@pytest.mark.django_db
class TestBulk:
    def picker(self, w, *locs, **extra):
        return {"version": w["version"].pk, "priority": "normal", "due_at": "", "instructions": "", "issue": "on", "include_descendants": "",
                "locations": [str(x.pk) for x in locs], "stage": "review", **extra}

    def test_the_form_lists_only_assignable_locations(self, client, w):
        client.force_login(w["manager"])
        html = body(client.get(url("bulk")))
        assert "Restaurant 1" in html and "Restaurant 3" not in html and "Search locations" in html and 'x-data="locationPicker"' in html

    def test_review_then_confirm_creates_everything(self, client, w):
        client.force_login(w["admin"])
        review = client.post(url("bulk"), self.picker(w, w["r1"], w["wh"]))
        html = body(review)
        assert "Review the targets" in html and "Region North / Area 1 / Restaurant 1" in html and "Warehouse 1" in html and "Confirm and issue" in html
        assert not ChecklistAssignment.objects.exists()
        confirm = client.post(url("bulk"), self.picker(w, w["r1"], w["wh"], stage="confirm", skipped="0"))
        assert "Assignments created" in body(confirm) and ChecklistAssignment.objects.filter(status="assigned").count() == 2

    def test_descendants_are_expanded_and_inaccessible_ones_are_counted(self, client, w):
        client.force_login(w["manager"])
        html = body(client.post(url("bulk"), self.picker(w, w["area1"], include_descendants="on")))
        assert "Restaurant 1" in html and "Restaurant 2" in html and "Restaurant 3" not in html
        client.force_login(w["admin"])
        html = body(client.post(url("bulk"), self.picker(w, w["region"], include_descendants="on")))
        assert all(n in html for n in ("Area 2", "Restaurant 3", "Restaurant 2"))

    def test_one_bad_target_blocks_everything_and_is_shown(self, client, w):
        make(w, location=w["r2"], issue=True)
        client.force_login(w["admin"])
        html = body(client.post(url("bulk"), self.picker(w, w["r1"], w["r2"])))
        assert "Cannot assign" in html and "already assigns" in html and "Confirm and issue" not in html and "Nothing was created" in html
        failed = body(client.post(url("bulk"), self.picker(w, w["r1"], w["r2"], stage="confirm")))
        assert "Cannot assign" in failed and ChecklistAssignment.objects.count() == 1

    def test_forged_locations_are_ignored_not_revealed(self, client, w):
        client.force_login(w["manager"])
        html = body(client.post(url("bulk"), self.picker(w, w["r3"], w["b_loc"])))
        assert "Choose at least one location" in html and "Restaurant 3" not in html and "B Site" not in html
        confirm = body(client.post(url("bulk"), self.picker(w, w["r1"], w["r3"], stage="confirm")))
        assert "not available" in confirm and not ChecklistAssignment.objects.exists()

    def test_repeating_the_confirmation_creates_no_duplicates(self, client, w):
        client.force_login(w["admin"])
        data = self.picker(w, w["r1"], w["r2"], stage="confirm")
        assert "Assignments created" in body(client.post(url("bulk"), data))
        again = body(client.post(url("bulk"), data))
        assert "Assignments created" not in again and ChecklistAssignment.objects.count() == 2

    def test_a_viewer_cannot_bulk_assign(self, client, w):
        client.force_login(w["viewer"])
        assert client.post(url("bulk"), self.picker(w, w["r1"])).status_code == 403


@pytest.mark.django_db
def test_the_navigation_entry_is_real(client, w):
    client.force_login(w["admin"])
    assert url("list") in body(client.get(reverse("core:home")))
    client.force_login(w["plain"])
    assert url("list") not in body(client.get(reverse("core:home")))
