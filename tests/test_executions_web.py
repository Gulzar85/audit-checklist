"""The execution screens: worklist, start/resume, workspace (full page and HTMX fragment), review, submit, detail, history, access."""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.core.models import ActivityLog
from apps.executions import services
from apps.executions.models import ChecklistExecution, ChecklistResponse

from executions_support import REQUIRED, build_exec_world, issued, item_id

HX = {"HTTP_HX_REQUEST": "true"}


def body(response) -> str:
    return response.content.decode()


def url(name, *args):
    return reverse(f"executions:{name}", args=args)


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_exec_world(org_a, org_b, make_user, assign, make_type)


def started(w, who="runner", **kw):
    ex, _created = services.start_or_resume(issued(w, **kw), actor=w[who])
    return ex


def form(w, ex, **values):
    """A workspace POST body: presence marker + current revision for every item given, then the values."""
    data = {"section": "FIRE", "action": "save"}
    revisions = {r.item.code: r.revision for r in ChecklistResponse.objects.filter(execution=ex).select_related("item")}
    for code, value in values.items():
        iid = item_id(w["eversion"], code)
        data[f"p__{iid}"] = "1"
        data[f"r__{iid}"] = str(revisions.get(code, 0))
        if isinstance(value, dict):
            for row, cell in value.items():
                data[f"m__{iid}__{row}"] = cell
        elif value is not None:
            data[f"v__{iid}"] = value
    return data


@pytest.mark.django_db
class TestAccess:
    def test_anonymous_is_redirected(self, client, w):
        ex = started(w)
        for name, args in (("list", ()), ("history", ()), ("workspace", (ex.pk,)), ("review", (ex.pk,)), ("detail", (ex.pk,))):
            assert client.get(url(name, *args)).status_code == 302, name

    def test_users_without_execution_permission_are_forbidden(self, client, w):
        client.force_login(w["plain"])
        assert client.get(url("list")).status_code == 403
        assert client.get(url("history")).status_code == 403

    def test_other_organizations_and_scopes_get_404(self, client, w):
        ex = started(w)
        client.force_login(w["other"])
        for name in ("workspace", "review", "detail"):
            assert client.get(url(name, ex.pk)).status_code in (403, 404), name
        assert client.post(url("submit", ex.pk)).status_code in (403, 404)
        assert client.post(url("cancel", ex.pk), {"reason": "x"}).status_code in (403, 404)
        assert client.post(url("workspace", ex.pk), form(w, ex, **{"FIRE-EXIT": "yes"})).status_code in (403, 404)
        assert not ex.responses.exists()
        assert client.get(url("detail", "00000000-0000-0000-0000-000000000000")).status_code in (403, 404)

    def test_the_worklist_shows_only_my_open_work(self, client, w):
        mine, other_loc = issued(w, w["r1"]), issued(w, w["r3"])
        client.force_login(w["runner"])
        html = body(client.get(url("list")))
        assert mine.reference in html and other_loc.reference not in html and "Execution Test Checklist" in html
        assert "Start" in html

    def test_start_is_post_only_and_scoped(self, client, w):
        a, foreign = issued(w, w["r1"]), issued(w, w["r3"])
        client.force_login(w["runner"])
        assert client.get(url("start", a.pk)).status_code == 405
        assert client.post(url("start", foreign.pk)).status_code == 404  # not open to this user: same as missing
        r = client.post(url("start", a.pk))
        ex = ChecklistExecution.objects.get()
        assert r.status_code == 302 and r["Location"] == url("workspace", ex.pk)
        assert client.post(url("start", a.pk))["Location"] == url("workspace", ex.pk)  # a repeat resumes
        assert ChecklistExecution.objects.count() == 1

    def test_csrf_is_enforced(self, w):
        from django.test import Client

        ex = started(w)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(w["runner"])
        assert strict.post(url("submit", ex.pk)).status_code == 403
        assert strict.post(url("workspace", ex.pk), form(w, ex, **{"FIRE-EXIT": "yes"})).status_code == 403
        assert not ex.responses.exists()

    def test_view_only_users_see_the_detail_but_cannot_work(self, client, w):
        ex = started(w)
        client.force_login(w["viewer"])  # assignment.view only: no execution.view
        assert client.get(url("detail", ex.pk)).status_code == 404
        client.force_login(w["manager"])  # execution.view at Area 1, not execution.update
        assert client.get(url("detail", ex.pk)).status_code == 200
        assert client.get(url("workspace", ex.pk))["Location"] == url("detail", ex.pk)
        assert client.post(url("submit", ex.pk)).status_code == 403
        assert client.post(url("workspace", ex.pk), form(w, ex, **{"FIRE-EXIT": "yes"})).status_code in (302, 403)  # never written
        assert not ex.responses.exists()

    def test_a_designated_assignment_is_invisible_in_the_workspace_to_others(self, client, w):
        ex = started(w, assigned_to=w["runner"])
        client.force_login(w["runner2"])
        assert client.get(url("workspace", ex.pk))["Location"] == url("detail", ex.pk)  # may view (same scope), may not work
        assert client.post(url("workspace", ex.pk), form(w, ex, **{"FIRE-EXIT": "yes"})).status_code in (302, 403)
        assert not ex.responses.exists()


@pytest.mark.django_db
class TestWorkspace:
    def test_the_workspace_renders_the_first_section(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        html = body(client.get(url("workspace", ex.pk)))
        for needle in ("Is the fire exit accessible?", "Is the handwashing station operational?", "Sections", "0% complete", "Save draft",
                       "Review before submit", "Next", "Version 1".lower()):
            assert needle.lower() in html.lower(), needle
        assert "Is the fire exit" in html and "Fridge temperature?" not in html  # one section at a time
        assert "Describe the issue" not in html  # hidden by the rule until the exit is blocked
        assert f'p__{item_id(w["eversion"], "FIRE-EXIT")}' in html

    def test_the_navigation_is_rendered_once_on_a_full_page(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        full = body(client.get(url("workspace", ex.pk)))
        assert full.count('id="exec-nav"') == 1 and "hx-swap-oob" not in full
        fragment = body(client.post(url("workspace", ex.pk), form(w, ex, **{"FIRE-EXIT": "yes"}), **HX))
        assert fragment.count('id="exec-nav"') == 1 and 'hx-swap-oob="true"' in fragment

    def test_other_sections_via_the_query_string(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        html = body(client.get(url("workspace", ex.pk) + "?section=FOOD"))
        assert "Fridge temperature?" in html and "Storage kind?" in html and 'type="number"' in html and "Area condition" in html
        assert "Photos, files and signatures" not in html
        assert "Photos, files and signatures" in body(client.get(url("workspace", ex.pk) + "?section=GEN"))

    def test_save_draft_stores_answers_and_redirects(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        r = client.post(url("workspace", ex.pk), form(w, ex, **{"FIRE-EXIT": "yes", "HANDWASH": "yes"}))
        assert r.status_code == 302 and "section=FIRE" in r["Location"]
        assert ChecklistResponse.objects.filter(execution=ex).count() == 2
        assert ActivityLog.objects.filter(verb="execution.responses_saved").count() == 1

    def test_htmx_autosave_returns_the_fragment_with_truthful_status(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        r = client.post(url("workspace", ex.pk), form(w, ex, **{"FIRE-EXIT": "no"}), **HX)
        html = body(r)
        assert r.status_code == 200 and "<html" not in html and 'id="exec-main"' in html and 'hx-swap-oob="true"' in html
        assert "Saved " in html and ChecklistResponse.objects.filter(execution=ex).count() == 1
        assert "Describe the issue" not in html  # other section: shown only when that section is open
        r = client.post(url("workspace", ex.pk), {**form(w, ex, **{"FIRE-EXIT": "no"}), "section": "GEN"}, **HX)
        assert "Describe the issue." in body(r) and "(required)" in body(r)  # the rule revealed and required it

    def test_the_fragment_carries_the_new_revision(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        client.post(url("workspace", ex.pk), form(w, ex, **{"FRIDGE": "4"}), **HX)
        html = body(client.get(url("workspace", ex.pk) + "?section=FOOD"))
        assert f'name="r__{item_id(w["eversion"], "FRIDGE")}" value="1"' in html

    def test_validation_errors_are_visible_and_the_input_is_kept(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        r = client.post(url("workspace", ex.pk), {**form(w, ex, **{"FRIDGE": "99"}), "section": "FOOD"})
        html = body(r)
        assert r.status_code == 200 and "at most 10" in html and 'value="99"' in html and "Saved " not in html
        assert not ex.responses.exists()

    def test_a_stale_revision_is_reported_and_nothing_is_overwritten(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        client.post(url("workspace", ex.pk), {**form(w, ex, **{"FRIDGE": "4"}), "section": "FOOD"})
        stale = {**form(w, ex, **{"FRIDGE": "8"}), "section": "FOOD"}
        stale[f"r__{item_id(w['eversion'], 'FRIDGE')}"] = "0"
        r = client.post(url("workspace", ex.pk), stale, **HX)
        assert "changed elsewhere" in body(r)
        assert str(ChecklistResponse.objects.get(execution=ex, item__code="FRIDGE").value).startswith("4")

    def test_forged_fields_cannot_write_hidden_foreign_or_media_items(self, client, w):
        from apps.checklists.models import ChecklistItem

        ex = started(w)
        client.force_login(w["runner"])
        foreign = str(ChecklistItem.objects.filter(section__version=w["version"]).first().pk)
        data = form(w, ex, **{"FIRE-EXIT": "yes", "ISSUE": "forged", "PHOTO": "x"})
        data[f"p__{foreign}"], data[f"v__{foreign}"] = "1", "yes"
        client.post(url("workspace", ex.pk), data)
        assert list(ChecklistResponse.objects.filter(execution=ex).values_list("item__code", flat=True)) == ["FIRE-EXIT"]

    def test_next_previous_and_review_actions(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        r = client.post(url("workspace", ex.pk), {**form(w, ex, **{"FIRE-EXIT": "yes"}), "action": "next"})
        assert r["Location"].endswith("?section=FOOD")
        r = client.post(url("workspace", ex.pk), {**form(w, ex), "section": "FOOD", "action": "prev"})
        assert r["Location"].endswith("?section=FIRE")
        r = client.post(url("workspace", ex.pk), {**form(w, ex), "action": "review"})
        assert r["Location"] == url("review", ex.pk)

    def test_user_text_is_escaped(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        client.post(url("workspace", ex.pk), {**form(w, ex, **{"FIRE-EXIT": "yes"}), f"c__{item_id(w['eversion'], 'FIRE-EXIT')}": "<script>alert(1)</script>"})
        html = body(client.get(url("workspace", ex.pk)))
        assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_a_submitted_execution_has_no_workspace(self, client, w):
        ex = started(w)
        services.save_answers(ex, actor=w["runner"], inputs=[services.AnswerInput(item_id(w["eversion"], c), v) for c, v in REQUIRED.items()])
        services.submit(ex, actor=w["runner"])
        client.force_login(w["runner"])
        assert client.get(url("workspace", ex.pk))["Location"] == url("detail", ex.pk)
        before = ChecklistResponse.objects.filter(execution=ex).count()
        r = client.post(url("workspace", ex.pk), form(w, ex, **{"FIRE-EXIT": "no"}))
        assert r.status_code == 302 and ChecklistResponse.objects.get(execution=ex, item__code="FIRE-EXIT").value == "yes"
        assert ChecklistResponse.objects.filter(execution=ex).count() == before


@pytest.mark.django_db
class TestReviewSubmitDetail:
    def fill(self, client, w, ex, **extra):
        values = {**REQUIRED, **extra}
        client.post(url("workspace", ex.pk), form(w, ex, **{k: v for k, v in values.items() if k in ("FIRE-EXIT", "HANDWASH")}))
        client.post(url("workspace", ex.pk), {**form(w, ex, FRIDGE=values["FRIDGE"]), "section": "FOOD"})

    def test_review_lists_blocking_problems_with_links(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        client.post(url("workspace", ex.pk), form(w, ex, **{"FIRE-EXIT": "yes"}))
        html = body(client.get(url("review", ex.pk)))
        assert "Fix these before submitting" in html and "An answer is required." in html
        assert f'?section=FIRE#item-{item_id(w["eversion"], "HANDWASH")}' in html and "Submit checklist" not in html
        assert client.post(url("submit", ex.pk))["Location"] == url("review", ex.pk)
        assert ChecklistExecution.objects.get(pk=ex.pk).status == "in_progress"

    def test_review_then_submit_then_read_only_detail(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        self.fill(client, w, ex)
        html = body(client.get(url("review", ex.pk)))
        assert "Ready to submit" in html and "Submit checklist" in html and "100%" in html
        r = client.post(url("submit", ex.pk), follow=True)
        page = body(r)
        assert "submitted" in page.lower() and "read-only" in page.lower() and "Is the fire exit accessible?" in page
        assert "4.5 C" in page and "Yes" in page
        ex.refresh_from_db()
        assert ex.status == "submitted" and ex.assignment.status == "completed"
        again = body(client.post(url("submit", ex.pk), follow=True))
        assert "already submitted" in again
        assert ActivityLog.objects.filter(verb="execution.submitted").count() == 1

    def test_the_detail_page_has_no_edit_controls(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        self.fill(client, w, ex)
        client.post(url("submit", ex.pk))
        html = body(client.get(url("detail", ex.pk)))
        assert "<textarea" not in html and 'type="radio"' not in html and url("workspace", ex.pk) not in html

    def test_hidden_answers_are_not_shown_after_submission(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        client.post(url("workspace", ex.pk), form(w, ex, **{"FIRE-EXIT": "no"}))
        client.post(url("workspace", ex.pk), {**form(w, ex, HANDWASH="yes"), "section": "FIRE"})
        client.post(url("workspace", ex.pk), {**form(w, ex, FRIDGE="4"), "section": "FOOD"})
        client.post(url("workspace", ex.pk), {**form(w, ex, ISSUE="Blocked by crates"), "section": "GEN"})
        client.post(url("workspace", ex.pk), form(w, ex, **{"FIRE-EXIT": "yes"}))
        client.post(url("submit", ex.pk))
        assert ChecklistResponse.objects.get(execution=ex, item__code="ISSUE").is_hidden
        assert "Blocked by crates" not in body(client.get(url("detail", ex.pk)))

    def test_cancel_with_a_reason(self, client, w):
        ex = started(w)
        client.force_login(w["admin"])
        assert client.post(url("cancel", ex.pk), {"reason": ""}, follow=True).status_code == 200
        assert ChecklistExecution.objects.get(pk=ex.pk).status == "in_progress"
        html = body(client.post(url("cancel", ex.pk), {"reason": "Wrong site"}, follow=True))
        assert "cancelled" in html.lower() and ChecklistExecution.objects.get(pk=ex.pk).status == "cancelled"
        client.force_login(w["runner"])
        assert client.post(url("cancel", ex.pk), {"reason": "x"}).status_code == 403  # the runner has no execution.cancel

    def test_history_lists_what_the_user_may_see(self, client, w):
        mine = started(w)
        client.force_login(w["runner"])
        html = body(client.get(url("history")))
        assert mine.reference in html
        assert mine.reference in body(client.get(url("history"), {"status": "in_progress"}))
        assert mine.reference not in body(client.get(url("history"), {"status": "submitted"}))
        client.force_login(w["other"])  # another organization: an empty list, never this execution
        assert mine.reference not in body(client.get(url("history")))
        client.force_login(w["plain"])
        assert client.get(url("history")).status_code == 403


@pytest.mark.django_db
class TestWorklist:
    def test_search_filters_progress_and_overdue(self, client, w):
        from apps.assignments.models import ChecklistAssignment
        from assignments_support import future

        a1, a2 = issued(w, w["r1"]), issued(w, w["r2"])
        client.force_login(w["area_runner"])
        services.start_or_resume(a1, actor=w["area_runner"])
        html = body(client.get(url("list")))
        assert a1.reference in html and a2.reference in html and "Continue" in html and "Start" in html and "0%" in html
        assert a2.reference not in body(client.get(url("list"), {"state": "in_progress"}))
        assert a1.reference in body(client.get(url("list"), {"state": "in_progress"}))
        assert a1.reference in body(client.get(url("list"), {"q": "restaurant 1"})) and a2.reference not in body(client.get(url("list"), {"q": "restaurant 1"}))
        ChecklistAssignment.objects.filter(pk=a2.pk).update(due_at=future(-3))
        assert a2.reference in body(client.get(url("list"), {"state": "overdue"})) and a1.reference not in body(client.get(url("list"), {"state": "overdue"}))

    def test_query_count_is_bounded(self, client, w, django_assert_max_num_queries):
        for loc in (w["r1"], w["r2"]):
            started(w, "area_runner", location=loc)
        client.force_login(w["area_runner"])
        client.get(url("list"))
        for _ in range(3):
            pass
        with django_assert_max_num_queries(60):
            client.get(url("list"))


@pytest.mark.django_db
def test_the_navigation_entry_is_real(client, w):
    client.force_login(w["runner"])
    assert url("list") in body(client.get(reverse("core:home")))
    client.force_login(w["plain"])
    assert url("list") not in body(client.get(reverse("core:home")))


