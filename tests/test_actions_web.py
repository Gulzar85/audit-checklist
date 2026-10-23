"""Phase 13 web UI: lists (all, mine, queue), detail, create/edit, lifecycle forms, work log, evidence views, admin, finding page, navigation."""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.corrective_actions import services
from apps.corrective_actions.models import CorrectiveAction, CorrectiveActionVerification
from apps.evidence.models import EvidenceAttachment

from actions_support import build_actions_world, future, make_action, make_finding, to_status
from evidence_support import evidence_storage, jpeg_bytes  # noqa: F401


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_actions_world(org_a, org_b, make_user, assign, make_type)


def get(client, user, url, **kw):
    client.force_login(user)
    return client.get(url, **kw)


def post(client, user, url, data=None, **kw):
    client.force_login(user)
    return client.post(url, data or {}, **kw)


@pytest.mark.django_db
class TestLists:
    def test_the_list_summary_filters_and_scope(self, client, w):
        make_action(w, title="Visible repair")
        make_action(w, make_finding(w, location=w["r2"], title="Elsewhere"), who="admin", owner=None, issue=False, title="Hidden elsewhere")
        html = get(client, w["aviewer"], reverse("corrective_actions:list")).content.decode()
        assert "Visible repair" in html and "Hidden elsewhere" not in html and "New action" not in html
        for name in ("status", "priority", "finding", "location", "assigned_to", "verification"):
            assert f'name="{name}"' in html, name
        assert "Hidden elsewhere" in get(client, w["admin"], reverse("corrective_actions:list")).content.decode()
        assert "No corrective actions" in get(client, w["admin"], reverse("corrective_actions:list"), data={"q": "zzz-none"}).content.decode()

    def test_search_filter_and_sort(self, client, w):
        make_action(w, title="Alpha job", priority="low")
        make_action(w, title="Beta job", priority="critical")
        url = reverse("corrective_actions:list")
        assert "Alpha job" not in get(client, w["admin"], url, data={"priority": "critical"}).content.decode()
        assert "Beta job" not in get(client, w["admin"], url, data={"q": "alpha"}).content.decode()
        html = get(client, w["admin"], url, data={"sort": "title"}).content.decode()
        assert html.index("Alpha job") < html.index("Beta job")
        assert "Alpha job" in get(client, w["admin"], url, data={"q": "CA-00001"}).content.decode()

    def test_my_actions(self, client, w):
        make_action(w, title="Mine")
        make_action(w, title="Theirs", owner="worker2")
        make_action(w, title="Draft of mine", issue=False)
        html = get(client, w["worker1"], reverse("corrective_actions:mine")).content.decode()
        assert "Mine" in html and "Theirs" not in html and "Draft of mine" not in html and "My corrective actions" in html

    def test_rejected_actions_are_called_out_on_the_worklist(self, client, w):
        to_status(w, make_action(w, title="Needs rework"), "rejected")
        html = get(client, w["worker1"], reverse("corrective_actions:mine")).content.decode()
        assert "Needs rework" in html and "need" in html and "your attention" in html

    def test_the_verification_queue(self, client, w):
        to_status(w, make_action(w, title="Waiting for review"), "submitted")
        own = to_status(w, make_action(w, title="My own work", owner="both"), "submitted", owner="both")
        html = get(client, w["verifier"], reverse("corrective_actions:queue")).content.decode()
        assert "Waiting for review" in html and "My own work" in html
        html = get(client, w["both"], reverse("corrective_actions:queue")).content.decode()
        assert "Waiting for review" in html and "My own work" not in html
        assert get(client, w["worker1"], reverse("corrective_actions:queue")).status_code == 403
        _ = own

    def test_access(self, client, w, make_user):
        nobody = make_user(email="ca-nobody@example.com", organization=w["org_a"], role=None)
        assert get(client, nobody, reverse("corrective_actions:list")).status_code == 403
        client.logout()
        assert client.get(reverse("corrective_actions:list")).status_code == 302

    def test_navigation(self, client, w):
        assert reverse("corrective_actions:list") in get(client, w["aviewer"], reverse("core:home")).content.decode()


@pytest.mark.django_db
class TestDetailPage:
    def test_detail_shows_plan_finding_status_and_only_the_permitted_actions(self, client, w):
        a = make_action(w, title="Fix it", plan="Order then fit", expected_outcome="No leak", due_at=future())
        html = get(client, w["worker1"], reverse("corrective_actions:detail", args=[a.pk])).content.decode()
        assert "Fix it" in html and "Order then fit" in html and "No leak" in html and a.finding.reference in html and "Start work" in html
        assert ">Approve<" not in html and ">Reject<" not in html and ">Close<" not in html
        viewer = get(client, w["aviewer"], reverse("corrective_actions:detail", args=[a.pk])).content.decode()
        assert "Start work" not in viewer and "Submit for verification" not in viewer and ">Edit<" not in viewer

    def test_invisible_actions_are_404(self, client, w):
        a = make_action(w)
        for user in ("b_admin", "r2_actor"):
            assert get(client, w[user], reverse("corrective_actions:detail", args=[a.pk])).status_code == 404

    def test_submitted_rejected_verified_are_clearly_different(self, client, w):
        s = to_status(w, make_action(w, title="Sub"), "submitted")
        html = get(client, w["verifier"], reverse("corrective_actions:detail", args=[s.pk])).content.decode()
        assert "awaiting independent verification. Not verified yet" in html and ">Approve<" in html and ">Reject<" in html
        owner_html = get(client, w["worker1"], reverse("corrective_actions:detail", args=[s.pk])).content.decode()
        assert ">Approve<" not in owner_html and ">Reject<" not in owner_html
        r = to_status(w, make_action(w, title="Rej"), "rejected")
        assert "Rejected: “Not tested under load.”" in get(client, w["worker1"], reverse("corrective_actions:detail", args=[r.pk])).content.decode()
        v = to_status(w, make_action(w, title="Ver"), "verified")
        html = get(client, w["manager"], reverse("corrective_actions:detail", args=[v.pk])).content.decode()
        assert "It is not closed yet, and the finding is unchanged." in html and "Checked on site" in html

    def test_the_owner_cannot_see_decision_buttons_for_their_own_work(self, client, w):
        a = make_action(w, owner="both")
        services.transition(a, "start", actor=w["both"])
        services.transition(a, "submit", actor=w["both"], reason="Done")
        html = get(client, w["both"], reverse("corrective_actions:detail", args=[a.pk])).content.decode()
        assert ">Approve<" not in html and ">Reject<" not in html and "separation of duties" in html

    def test_overdue_and_blocked_banners(self, client, w):
        from datetime import timedelta

        from django.utils import timezone

        a = to_status(w, make_action(w), "blocked")
        CorrectiveAction.objects.filter(pk=a.pk).update(due_at=timezone.now() - timedelta(days=1))
        html = get(client, w["worker1"], reverse("corrective_actions:detail", args=[a.pk])).content.decode()
        assert "This action is overdue" in html and "Blocked since" in html and "Waiting for a part" in html


@pytest.mark.django_db
class TestFormsAndActions:
    def test_create_from_a_finding(self, client, w):
        f = make_finding(w)
        client.force_login(w["manager"])
        page = client.get(reverse("corrective_actions:create"), {"finding": f.pk})
        assert page.status_code == 200 and "New corrective action" in page.content.decode()
        r = client.post(reverse("corrective_actions:create"), {"finding": f.pk, "title": "From the form", "priority": "normal", "assigned_to": w["worker1"].pk, "issue": "1"})
        a = CorrectiveAction.objects.get(title="From the form")
        assert r.status_code == 302 and r.url == reverse("corrective_actions:detail", args=[a.pk]) and a.status == "open" and a.created_by == w["manager"]

    def test_invalid_and_forged_input_creates_nothing(self, client, w):
        f = make_finding(w)
        client.force_login(w["manager"])
        r = client.post(reverse("corrective_actions:create"), {"finding": f.pk, "title": "", "priority": "normal"})
        assert r.status_code == 200 and not CorrectiveAction.objects.exists()
        r = client.post(reverse("corrective_actions:create"), {"finding": f.pk, "title": "x", "priority": "normal", "issue": "1"})  # issue without an owner
        assert r.status_code == 200 and "Assign a responsible user" in r.content.decode() and not CorrectiveAction.objects.exists()
        foreign = make_finding(w, location=w["r2"], title="Elsewhere")
        r = client.post(reverse("corrective_actions:create"), {"finding": foreign.pk, "title": "x", "priority": "normal"})
        assert r.status_code == 200 and not CorrectiveAction.objects.exists()
        r = client.post(reverse("corrective_actions:create"), {"finding": f.pk, "title": "x", "priority": "normal", "assigned_to": w["aviewer"].pk})
        assert r.status_code == 200 and not CorrectiveAction.objects.exists()

    def test_create_is_forbidden_without_the_permission(self, client, w):
        client.force_login(w["aviewer"])
        assert client.get(reverse("corrective_actions:create")).status_code == 403
        assert client.post(reverse("corrective_actions:create"), {}).status_code == 403

    def test_assignee_options_list_eligible_users_only(self, client, w):
        f = make_finding(w)
        html = get(client, w["manager"], reverse("corrective_actions:assignee_options"), data={"finding": f.pk}).content.decode()
        assert w["worker1"].email in html and w["aviewer"].email not in html and w["verifier"].email not in html

    def test_edit_form_shapes_itself_to_the_role(self, client, w):
        a = make_action(w)
        owner_page = get(client, w["worker1"], reverse("corrective_actions:edit", args=[a.pk])).content.decode()
        assert 'name="plan"' in owner_page and 'name="due_at"' not in owner_page and 'name="priority"' not in owner_page
        manager_page = get(client, w["manager"], reverse("corrective_actions:edit", args=[a.pk])).content.decode()
        assert 'name="due_at"' in manager_page and 'name="priority"' in manager_page
        client.force_login(w["manager"])
        client.post(reverse("corrective_actions:edit", args=[a.pk]), {"title": "Edited", "description": "", "plan": "New plan", "expected_outcome": "", "completion_criteria": "",
                                                                      "priority": "high", "required_evidence": 1})
        a.refresh_from_db()
        assert (a.title, a.plan, a.priority, a.required_evidence) == ("Edited", "New plan", "high", 1)
        assert get(client, w["aviewer"], reverse("corrective_actions:edit", args=[a.pk])).status_code == 403

    def test_the_lifecycle_forms(self, client, w):
        a = make_action(w)
        client.force_login(w["worker1"])
        assert "is now in progress" in client.post(reverse("corrective_actions:start", args=[a.pk]), follow=True).content.decode()
        assert "Describe the blocker" in client.post(reverse("corrective_actions:block", args=[a.pk]), {"reason": ""}, follow=True).content.decode()
        assert "already in progress" in client.post(reverse("corrective_actions:start", args=[a.pk]), follow=True).content.decode()
        client.post(reverse("corrective_actions:submit", args=[a.pk]), {"reason": "Done"})
        assert CorrectiveAction.objects.get(pk=a.pk).status == "submitted"
        assert client.post(reverse("corrective_actions:verify", args=[a.pk])).status_code == 403  # the owner cannot decide
        client.force_login(w["verifier"])
        client.post(reverse("corrective_actions:reject", args=[a.pk]), {"reason": ""}, follow=True)
        assert CorrectiveAction.objects.get(pk=a.pk).status == "submitted"  # a rejection needs a reason
        client.post(reverse("corrective_actions:verify", args=[a.pk]), {"reason": "Looks right"})
        assert CorrectiveAction.objects.get(pk=a.pk).status == "verified" and CorrectiveActionVerification.objects.get(action=a).notes == "Looks right"
        client.force_login(w["manager"])
        client.post(reverse("corrective_actions:close", args=[a.pk]))
        assert CorrectiveAction.objects.get(pk=a.pk).status == "closed"

    def test_actions_are_post_only_scoped_and_csrf_protected(self, client, w):
        from django.test import Client

        a = make_action(w)
        client.force_login(w["worker1"])
        assert client.get(reverse("corrective_actions:start", args=[a.pk])).status_code == 405
        client.force_login(w["b_admin"])
        assert client.post(reverse("corrective_actions:start", args=[a.pk])).status_code == 404
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(w["worker1"])
        assert strict.post(reverse("corrective_actions:start", args=[a.pk])).status_code == 403
        assert CorrectiveAction.objects.get(pk=a.pk).status == "open"

    def test_assign_and_progress_forms(self, client, w):
        a = make_action(w, owner=None, issue=False)
        client.force_login(w["manager"])
        client.post(reverse("corrective_actions:assign", args=[a.pk]), {"assigned_to": w["worker1"].pk})
        a.refresh_from_db()
        assert a.assigned_to == w["worker1"]
        client.post(reverse("corrective_actions:assign", args=[a.pk]), {"assigned_to": w["aviewer"].pk}, follow=True)
        a.refresh_from_db()
        assert a.assigned_to == w["worker1"]  # an ineligible user is refused
        to_status(w, make_action(w, title="Working"), "in_progress")
        b = CorrectiveAction.objects.get(title="Working")
        client.force_login(w["worker1"])
        r = client.post(reverse("corrective_actions:progress", args=[b.pk]), {"body": "Seal ordered", "progress_percent": 40}, follow=True)
        assert "Progress recorded" in r.content.decode() and b.updates.filter(kind="progress", progress_percent=40).exists()
        bad = client.post(reverse("corrective_actions:progress", args=[b.pk]), {"body": "x", "progress_percent": 250}, follow=True)
        assert b.updates.count() == 1 and bad.status_code == 200


@pytest.mark.django_db
class TestEvidenceViews:
    def upload(self, client, w, a, who="worker1", purpose="completion"):
        client.force_login(w[who])
        return client.post(reverse("corrective_actions:evidence_upload", args=[a.pk]), {"purpose": purpose, "file": SimpleUploadedFile("proof.jpg", jpeg_bytes())}, follow=True)

    def test_upload_download_preview_and_detach(self, client, w):
        a = to_status(w, make_action(w), "in_progress")
        assert "File uploaded" in self.upload(client, w, a).content.decode()
        att = EvidenceAttachment.objects.get(corrective_action=a)
        assert (att.purpose, att.target_type) == ("completion", "corrective_action")
        detail = get(client, w["aviewer"], reverse("corrective_actions:detail", args=[a.pk])).content.decode()
        assert "proof.jpg" in detail and "Available and verified" in detail and reverse("corrective_actions:evidence_preview", args=[a.pk, att.pk]) in detail
        d = get(client, w["aviewer"], reverse("corrective_actions:evidence_download", args=[a.pk, att.pk]))
        assert d.status_code == 200 and d["Content-Disposition"].startswith("attachment") and d["X-Content-Type-Options"] == "nosniff"
        assert get(client, w["aviewer"], reverse("corrective_actions:evidence_preview", args=[a.pk, att.pk]))["Content-Disposition"].startswith("inline")
        assert get(client, w["b_admin"], reverse("corrective_actions:evidence_download", args=[a.pk, att.pk])).status_code == 404
        client.force_login(w["worker1"])
        client.post(reverse("corrective_actions:evidence_detach", args=[a.pk, att.pk]))
        assert EvidenceAttachment.objects.get(pk=att.pk).detached_at is not None  # detached, never deleted

    def test_a_file_of_another_action_is_not_reachable_through_this_one(self, client, w):
        a = to_status(w, make_action(w), "in_progress")
        other = make_action(w, title="Other")
        self.upload(client, w, a)
        att = EvidenceAttachment.objects.get(corrective_action=a)
        assert get(client, w["manager"], reverse("corrective_actions:evidence_download", args=[other.pk, att.pk])).status_code == 404

    def test_stage_rules_and_errors_are_shown(self, client, w):
        a = make_action(w)  # open: not started, so no completion files yet
        r = self.upload(client, w, a)
        assert "cannot be added" in r.content.decode() and not EvidenceAttachment.objects.exists()
        s = to_status(w, make_action(w, title="Sub"), "submitted")
        assert "cannot be added" in self.upload(client, w, s).content.decode()
        assert "File uploaded" in self.upload(client, w, s, who="verifier", purpose="verification").content.decode()


@pytest.mark.django_db
class TestFindingPage:
    def test_the_finding_lists_its_actions_and_eligibility(self, client, w):
        f = make_finding(w)
        make_action(w, f, title="Repair")
        html = get(client, w["manager"], reverse("findings:detail", args=[f.pk])).content.decode()
        assert "Corrective actions" in html and "Repair" in html and "Actions still outstanding" in html and "CA-00001" in html and "Add action" in html
        assert "Verifying an action never resolves or closes the finding by itself." in html


@pytest.mark.django_db
class TestAdmin:
    def test_the_admin_is_read_only_and_history_is_protected(self, client, w, django_user_model):
        a = to_status(w, make_action(w), "verified")
        su = django_user_model.objects.create_superuser(email="su-ca@example.com", password="x-Strong-pass-123")
        client.force_login(su)
        for model in ("correctiveaction", "correctiveactionupdate", "correctiveactionverification"):
            assert client.get(reverse(f"admin:corrective_actions_{model}_changelist")).status_code == 200
            assert client.get(reverse(f"admin:corrective_actions_{model}_add")).status_code == 403
        page = client.get(reverse("admin:corrective_actions_correctiveaction_change", args=[a.pk])).content.decode()
        assert 'name="_save"' not in page and a.title in page
        assert client.post(reverse("admin:corrective_actions_correctiveaction_delete", args=[a.pk]), {"post": "yes"}).status_code == 403
        assert client.post(reverse("admin:corrective_actions_correctiveaction_change", args=[a.pk]), {"status": "closed", "title": "hacked"}).status_code in (302, 403)
        v = a.verifications.first()
        assert client.post(reverse("admin:corrective_actions_correctiveactionverification_change", args=[v.pk]), {"decision": "rejected"}).status_code in (302, 403)
        a.refresh_from_db()
        assert a.status == "verified" and a.title != "hacked" and a.verifications.first().decision == "approved"

    def test_a_scoped_staff_member_only_sees_their_scope(self, w):
        from django.contrib import admin
        from django.test import RequestFactory

        from apps.corrective_actions.admin import CorrectiveActionAdmin

        a = make_action(w)
        elsewhere = make_action(w, make_finding(w, location=w["r2"], title="Elsewhere"), who="admin", owner=None, issue=False, title="Other place")
        w["aviewer"].is_staff = True
        w["aviewer"].save()
        request = RequestFactory().get("/")
        request.user = w["aviewer"]
        assert set(CorrectiveActionAdmin(CorrectiveAction, admin.site).get_queryset(request).values_list("pk", flat=True)) == {a.pk}
        _ = elsewhere
