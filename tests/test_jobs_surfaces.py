"""Phase 18 surfaces: web screens, REST API, commands, admin and the Phase 17 integration (handlers call the existing notification services)."""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.jobs import constants as k
from apps.jobs import registry, services as js
from apps.jobs.models import RunnerHeartbeat, ScheduleDefinition, ScheduledJob
from apps.notifications.models import Notification, NotificationEvent
from apps.permissions import services as rbac
from apps.permissions.registry import P

from actions_support import make_action, to_status
from jobs_support import build_jobs_world, reset
from notification_support import build_notification_world

API = "/api/v1/scheduling/"
SCHEDULE = {"name": "Every 15", "task_type": "test.echo", "schedule_type": "interval", "timezone": "UTC", "recurrence": {"every_minutes": 15}, "payload": {"n": 1}}


@pytest.fixture
def w(org_a, org_b, make_user, assign, superuser):
    return build_jobs_world(org_a, org_b, make_user, assign, superuser)


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


def payload(**over):
    return {**SCHEDULE, "start_at": (timezone.now() + timedelta(minutes=1)).isoformat(), **over}


def run_all():
    results = []
    for pk, token in js.claim(50, runner="t"):
        results.append(js.execute(pk, token))
    return results


# --------------------------------------------------------------------------------------------------------------------------------- web
@pytest.mark.django_db
class TestWeb:
    def test_anonymous_and_unauthorized_users_are_refused(self, w):
        for name in ("jobs:dashboard", "jobs:schedules", "jobs:jobs", "jobs:diagnostics", "jobs:schedule_new"):
            assert Client().get(reverse(name)).status_code == 302
            assert web(w["nobody"]).get(reverse(name)).status_code == 403

    def test_dashboard_and_lists_show_real_data(self, w):
        s = js.save_schedule(w["manager"], w["org_a"], {**SCHEDULE, "start_at": timezone.now()})
        js.create_manual(w["manager"], "test.echo", w["org_a"], {})
        page = web(w["viewer"]).get(reverse("jobs:dashboard"))
        assert page.status_code == 200 and s.name in page.content.decode()
        assert s.name in web(w["viewer"]).get(reverse("jobs:schedules")).content.decode()
        assert "test.echo" in web(w["viewer"]).get(reverse("jobs:jobs")).content.decode()
        assert s.name not in web(w["other"]).get(reverse("jobs:schedules")).content.decode()  # another organization sees nothing

    def test_cross_organization_detail_is_404(self, w):
        s = js.save_schedule(w["manager"], w["org_a"], {**SCHEDULE, "start_at": timezone.now()})
        job = js.create_manual(w["manager"], "test.echo", w["org_a"], {})
        assert web(w["other"]).get(reverse("jobs:schedule", args=[s.pk])).status_code == 404
        assert web(w["other"]).get(reverse("jobs:job", args=[job.pk])).status_code == 404
        assert web(w["other"]).post(reverse("jobs:job_action", args=[job.pk, "cancel"]), {"reason": "no way"}).status_code == 404

    def test_create_edit_and_controls_through_the_form(self, w):
        c = web(w["manager"])
        form = {"name": "Web daily", "task_type": "test.echo", "schedule_type": "daily", "timezone": "Asia/Karachi", "start_at": "2030-01-01T00:00", "time": "09:00", "misfire_policy": "skip", "max_attempts": 3, "payload": ""}
        r = c.post(reverse("jobs:schedule_new"), form)
        assert r.status_code == 302
        s = ScheduleDefinition.objects.get(name="Web daily")
        assert (s.organization, s.timezone, s.recurrence) == (w["org_a"], "Asia/Karachi", {"time": "09:00"}) and s.next_run_at.hour == 4  # 09:00 PKT
        bad = c.post(reverse("jobs:schedule_new"), {**form, "name": "Bad", "time": "9am"})
        assert bad.status_code == 400 and not ScheduleDefinition.objects.filter(name="Bad").exists()
        assert c.post(reverse("jobs:schedule_action", args=[s.pk, "pause"])).status_code == 302
        s.refresh_from_db()
        assert s.status == "paused"
        assert c.get(reverse("jobs:schedule_edit", args=[s.pk])).status_code == 200

    def test_viewers_cannot_change_anything(self, w):
        s = js.save_schedule(w["manager"], w["org_a"], {**SCHEDULE, "start_at": timezone.now()})
        c = web(w["viewer"])
        assert c.post(reverse("jobs:schedule_action", args=[s.pk, "pause"])).status_code == 403
        assert c.post(reverse("jobs:schedule_action", args=[s.pk, "run"])).status_code == 403
        s.refresh_from_db()
        assert s.status == "active" and not ScheduledJob.objects.exists()

    def test_cancel_and_retry_need_a_reason_and_csrf(self, w):
        job = js.create_manual(w["manager"], "test.echo", w["org_a"], {})
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(w["manager"])
        assert strict.post(reverse("jobs:job_action", args=[job.pk, "cancel"]), {"reason": "x"}).status_code == 403  # CSRF
        c = web(w["manager"])
        c.post(reverse("jobs:job_action", args=[job.pk, "cancel"]), {"reason": ""})
        job.refresh_from_db()
        assert job.status == "pending"
        c.post(reverse("jobs:job_action", args=[job.pk, "cancel"]), {"reason": "not needed"})
        job.refresh_from_db()
        assert job.status == "cancelled"

    def test_job_pages_never_show_payloads_tokens_or_tracebacks(self, w):
        reset(ValueError("Traceback secret-token-xyz"))
        job = js.create_manual(w["manager"], "test.echo", w["org_a"], {"n": 7})
        run_all()
        html = web(w["manager"]).get(reverse("jobs:job", args=[job.pk])).content.decode()
        assert "secret-token-xyz" not in html and "Traceback" not in html and "lease" not in html.lower().replace("expired lease", "")

    def test_diagnostics_is_for_superusers_and_separates_no_runner_from_idle(self, w):
        assert web(w["manager"]).get(reverse("jobs:diagnostics")).status_code == 403
        page = web(w["super"]).get(reverse("jobs:diagnostics")).content.decode()
        assert "No runner has ever reported" in page
        RunnerHeartbeat.objects.create(runner="r1", last_seen_at=timezone.now())
        page = web(w["super"]).get(reverse("jobs:diagnostics")).content.decode()
        assert "Runner active" in page and "idle" in page
        RunnerHeartbeat.objects.filter(runner="r1").update(last_seen_at=timezone.now() - timedelta(hours=2))
        assert "Runner silent" in web(w["super"]).get(reverse("jobs:diagnostics")).content.decode()


# --------------------------------------------------------------------------------------------------------------------------------- API
@pytest.mark.django_db
class TestApi:
    def test_authentication_and_visibility(self, w):
        assert api().get(API + "schedules/").status_code in (401, 403)
        s = js.save_schedule(w["manager"], w["org_a"], {**SCHEDULE, "start_at": timezone.now()})
        assert api(w["viewer"]).get(API + "schedules/").json()["meta"]["count"] == 1
        assert api(w["other"]).get(API + "schedules/").json()["meta"]["count"] == 0
        assert api(w["other"]).get(f"{API}schedules/{s.pk}/").status_code == 404
        assert api(w["nobody"]).get(API + "schedules/").json()["meta"]["count"] == 0

    def test_create_validate_update_and_state_changes(self, w):
        c = api(w["manager"])
        made = c.post(API + "schedules/", payload(), format="json")
        assert made.status_code == 201
        sid = body(made)["id"]
        assert c.post(API + "schedules/", payload(task_type="os.system"), format="json").status_code == 400
        assert c.post(API + "schedules/", payload(name="x2", payload={"evil": 1}), format="json").status_code == 400
        assert c.post(API + "schedules/", payload(name="x3", **{"status": "active"}), format="json").status_code == 400  # unknown field
        assert c.post(API + "schedules/", payload(name="x4", timezone="Nope/Zone"), format="json").status_code == 400
        assert c.post(API + "schedules/", payload(name="x5", task_type="test.global_only"), format="json").status_code == 403
        assert body(c.put(f"{API}schedules/{sid}/", {"name": "Renamed"}, format="json"))["name"] == "Renamed"
        assert c.put(f"{API}schedules/{sid}/", {"timezone": "Asia/Karachi"}, format="json").status_code == 400  # needs confirmation
        assert body(c.post(f"{API}schedules/{sid}/pause/"))["status"] == "paused"
        assert c.post(f"{API}schedules/{sid}/pause/").status_code == 409
        assert body(c.post(f"{API}schedules/{sid}/resume/"))["status"] == "active"
        assert c.post(f"{API}schedules/{sid}/explode/").status_code == 404
        assert api(w["viewer"]).post(f"{API}schedules/{sid}/pause/").status_code == 403

    def test_run_now_is_idempotent_and_permissioned(self, w):
        c = api(w["manager"])
        sid = body(c.post(API + "schedules/", payload(), format="json"))["id"]
        first = c.post(f"{API}schedules/{sid}/run/", HTTP_IDEMPOTENCY_KEY="k1")
        second = c.post(f"{API}schedules/{sid}/run/", HTTP_IDEMPOTENCY_KEY="k1")
        assert first.status_code == second.status_code == 202 and body(first)["id"] == body(second)["id"] and ScheduledJob.objects.count() == 1
        assert api(w["viewer"]).post(f"{API}schedules/{sid}/run/").status_code == 403
        assert c.post(API + "jobs/", {"task_type": "os.system"}, format="json").status_code == 400
        assert c.post(API + "jobs/", {"task_type": "test.echo", "bogus": 1}, format="json").status_code == 400
        assert c.post(API + "jobs/", {"task_type": "test.global_only", "platform_wide": True}, format="json").status_code == 403
        assert api(w["super"]).post(API + "jobs/", {"task_type": "test.global_only", "platform_wide": True}, format="json").status_code == 202

    def test_jobs_listing_filters_pagination_and_no_payload_leak(self, w):
        c = api(w["manager"])
        for i in range(3):
            js.create_manual(w["manager"], "test.echo", w["org_a"], {"n": i})
        page = c.get(API + "jobs/?limit=2&status=pending").json()
        assert page["meta"]["count"] == 3 and len(page["data"]) == 2
        assert "payload" not in page["data"][0] and "lease_token" not in page["data"][0]
        assert c.get(API + "jobs/?limit=abc").status_code == 400
        assert c.get(API + "jobs/?status=nonsense").json()["meta"]["count"] == 3  # an unknown filter value is ignored, never executed

    def test_cancel_and_retry_through_the_api(self, w):
        job = js.create_manual(w["manager"], "test.echo", w["org_a"], {})
        c = api(w["manager"])
        assert c.post(f"{API}jobs/{job.pk}/cancel/", {}, format="json").status_code == 400
        assert api(w["viewer"]).post(f"{API}jobs/{job.pk}/cancel/", {"reason": "nope nope"}, format="json").status_code == 403
        assert body(c.post(f"{API}jobs/{job.pk}/cancel/", {"reason": "not needed"}, format="json"))["status"] == "cancelled"
        assert c.post(f"{API}jobs/{job.pk}/cancel/", {"reason": "twice now"}, format="json").status_code == 409
        assert c.post(f"{API}jobs/{job.pk}/retry/", {"reason": "retry it"}, format="json").status_code == 409  # only failed jobs

    def test_detail_includes_history_and_diagnostics_are_superuser_only(self, w):
        reset({"ok": 1})
        job = js.create_manual(w["manager"], "test.echo", w["org_a"], {})
        run_all()
        detail = body(api(w["manager"]).get(f"{API}jobs/{job.pk}/"))
        assert detail["status"] == "succeeded" and detail["executions"][0]["outcome"] == "succeeded"
        assert api(w["manager"]).get(API + "diagnostics/").status_code == 403
        d = body(api(w["super"]).get(API + "diagnostics/"))
        assert d["runner_state"] == "never_seen"

    def test_registry_is_listed_with_what_the_caller_may_do(self, w):
        tasks = {t["code"]: t for t in body(api(w["manager"]).get(API + "tasks/"))["tasks"]}
        assert tasks["test.echo"]["can_trigger"] and not tasks["test.global_only"]["can_trigger"]
        assert not body(api(w["viewer"]).get(API + "tasks/"))["tasks"][0]["can_schedule"]

    def test_the_web_and_the_api_share_the_service_layer(self, w):
        data = {"name": "Both", "task_type": "test.echo", "schedule_type": "daily", "timezone": "UTC", "start_at": timezone.now() + timedelta(days=1), "recurrence": {"time": "01:00"}}
        a = js.save_schedule(w["manager"], w["org_a"], data)
        b = body(api(w["manager"]).post(API + "schedules/", payload(name="Both API", schedule_type="daily", recurrence={"time": "01:00"}), format="json"))
        assert a.next_run_at.hour == 1 and b["next_run_at"].endswith("01:00:00Z") or "T01:00" in b["next_run_at"]


# ------------------------------------------------------------------------------------------------------------------------ commands/admin
@pytest.mark.django_db
class TestCommands:
    def run(self, *args):
        out = io.StringIO()
        call_command(*args, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def test_the_runner_processes_due_work_and_is_bounded_and_idempotent(self, w):
        js.save_schedule(w["manager"], w["org_a"], {**SCHEDULE, "start_at": timezone.now() - timedelta(hours=1), "recurrence": {"every_minutes": 5}}, now=timezone.now() - timedelta(hours=1))
        ScheduleDefinition.objects.update(next_run_at=timezone.now() - timedelta(seconds=30))
        assert "ran=1" in self.run("process_scheduled_jobs", "--limit", "5", "--runner-id", "cmd")
        assert ScheduledJob.objects.get().status == "succeeded"
        assert "ran=0" in self.run("process_scheduled_jobs")
        assert RunnerHeartbeat.objects.get(runner="cmd").jobs_processed == 1
        for bad in (["process_scheduled_jobs", "--limit", "0"], ["process_scheduled_jobs", "--poll-interval", "1"], ["process_scheduled_jobs", "--max-seconds", "99999"]):
            with pytest.raises(CommandError):
                self.run(*bad)

    def test_dry_run_changes_nothing(self, w):
        js.create_manual(w["manager"], "test.echo", w["org_a"], {})
        assert "1 job(s) ready" in self.run("process_scheduled_jobs", "--dry-run")
        assert ScheduledJob.objects.get().status == "pending"

    def test_run_scheduled_job_and_retry_failed_jobs(self, w):
        reset(registry.PermanentError("nope") if hasattr(registry, "PermanentError") else __import__("apps.jobs.errors", fromlist=["x"]).PermanentError("nope"))
        job = js.create_manual(w["manager"], "test.echo", w["org_a"], {})
        assert "failed" in self.run("run_scheduled_job", str(job.pk))
        with pytest.raises(CommandError):
            self.run("run_scheduled_job", str(job.pk))  # no longer runnable
        reset()
        assert "Re-queued 1" in self.run("retry_failed_jobs", "--task", "test.echo")
        job.refresh_from_db()
        assert job.status == "pending"

    def test_inspect_reports_a_missing_runner_with_a_failing_exit_code(self, w):
        js.create_manual(w["manager"], "test.echo", w["org_a"], {})
        with pytest.raises(SystemExit) as info:
            self.run("inspect_scheduled_jobs")
        assert info.value.code == 1
        RunnerHeartbeat.objects.create(runner="r", last_seen_at=timezone.now())
        ScheduledJob.objects.all().delete()
        assert "Runner: alive" in self.run("inspect_scheduled_jobs")

    def test_default_schedules_are_seeded_once(self, w):
        assert "Created 4" in self.run("seed_default_schedules")
        assert "Created 0" in self.run("seed_default_schedules")
        assert set(ScheduleDefinition.objects.values_list("task_type", flat=True)) >= {"notifications.process_pending", "jobs.cleanup_old_execution_history"}

    def test_admin_is_read_only(self, admin_client):
        for model in ("scheduledefinition", "scheduledjob", "jobexecution", "runnerheartbeat"):
            assert admin_client.get(f"/admin/jobs/{model}/").status_code == 200
            assert admin_client.get(f"/admin/jobs/{model}/add/").status_code == 403


# ------------------------------------------------------------------------------------------------------------ Phase 17 integration
@pytest.fixture
def nw(org_a, org_b, make_user, assign, make_type, superuser):
    world = build_notification_world(org_a, org_b, make_user, assign, make_type)
    world["super"] = superuser
    role = rbac.create_role(organization=org_a, name="Jobs mgr", permissions=[P.JOBS_VIEW, P.JOBS_MANAGE, P.JOBS_RUN])
    user = make_user(email="nj-manager@example.com", organization=org_a, role=None)
    assign(user, org_a, role)
    world["jobs_manager"] = user
    return world


@pytest.mark.django_db
class TestNotificationIntegration:
    def test_the_registered_tasks_are_exactly_the_reviewed_ones(self):
        assert {"notifications.process_pending", "notifications.dispatch_retries", "notifications.purge_history", "corrective_actions.generate_due_reminders",
                "jobs.cleanup_old_execution_history"} <= set(registry.REGISTRY)
        for code in ("checklists.generate_due_reminders", "workflow_tasks.generate_pending_reminders", "notifications.retry_failed"):
            assert code not in registry.REGISTRY  # no such service / event exists: not invented

    def test_process_pending_reuses_the_notification_services(self, nw, monkeypatch):
        from apps.notifications import services as ns

        action = make_action(nw)
        event = ns.publish("corrective_action.assigned", action, actor=nw["manager"], discriminator="job1", process=False)
        assert event.status == "pending" and not Notification.objects.exists()
        calls = []
        real = ns.dispatch_batch
        monkeypatch.setattr(ns, "dispatch_batch", lambda *a, **kw: calls.append(a) or real(*a, **kw))
        job = js.create_manual(nw["super"], "notifications.process_pending", None, {"limit": 50})
        assert run_all() == ["succeeded"] and calls
        event.refresh_from_db()
        job.refresh_from_db()
        assert event.status == "processed" and Notification.objects.filter(event=event).exists()
        assert job.result["events_processed"] >= 1  # counts only: nothing claims delivery

    def test_the_job_result_never_claims_delivery(self, nw):
        job = js.create_manual(nw["super"], "notifications.dispatch_retries", None, {})
        run_all()
        job.refresh_from_db()
        assert job.status == "succeeded" and not any(key in job.result for key in ("delivered", "confirmed"))

    def test_platform_tasks_are_refused_to_organization_admins(self, nw):
        from django.core.exceptions import PermissionDenied

        with pytest.raises(PermissionDenied):
            js.create_manual(nw["jobs_manager"], "notifications.process_pending", None, {})
        with pytest.raises(PermissionDenied):
            js.create_manual(nw["jobs_manager"], "notifications.process_pending", nw["org_a"], {})

    def test_corrective_action_reminders_are_idempotent_and_exclude_ineligible_records(self, nw):
        due = make_action(nw, due_at=timezone.now() + timedelta(hours=10))
        done = make_action(nw, due_at=timezone.now() + timedelta(hours=10), title="Done one")
        to_status(nw, done, "closed")
        done.refresh_from_db()
        assert done.status == "closed"
        js.create_manual(nw["jobs_manager"], "corrective_actions.generate_due_reminders", nw["org_a"], {"within_hours": 48})
        run_all()
        keys = list(NotificationEvent.objects.filter(event_type="corrective_action.due_soon").values_list("source_id", flat=True))
        assert keys == [due.pk]  # the verified (finished) action gets no reminder
        js.create_manual(nw["jobs_manager"], "corrective_actions.generate_due_reminders", nw["org_a"], {"within_hours": 48})
        run_all()
        assert NotificationEvent.objects.filter(event_type="corrective_action.due_soon").count() == 1  # a repeated job (or retry) creates no duplicate event

    def test_organization_scoped_reminders_stay_in_their_organization(self, nw):
        make_action(nw, due_at=timezone.now() + timedelta(hours=10))
        job = js.create_manual(nw["super"], "corrective_actions.generate_due_reminders", nw["org_b"], {"within_hours": 48})
        run_all()
        job.refresh_from_db()
        assert job.status == "succeeded" and not NotificationEvent.objects.filter(event_type="corrective_action.due_soon").exists()

    def test_a_scheduled_reminder_run_goes_through_the_runner_end_to_end(self, nw):
        make_action(nw, due_at=timezone.now() + timedelta(hours=10))
        js.save_schedule(nw["jobs_manager"], nw["org_a"], {"name": "Reminders", "task_type": "corrective_actions.generate_due_reminders", "schedule_type": "daily", "timezone": "UTC",
                                                           "start_at": timezone.now() + timedelta(minutes=1), "recurrence": {"time": "07:00"}})
        ScheduleDefinition.objects.update(next_run_at=timezone.now() - timedelta(seconds=5))
        summary = js.run_once(limit=5, runner="e2e")
        assert summary["succeeded"] == 1 and NotificationEvent.objects.filter(event_type="corrective_action.due_soon").count() == 1

    def test_retention_handlers_are_bounded_and_permission_protected(self, nw):
        from django.core.exceptions import PermissionDenied

        job = js.create_manual(nw["super"], "jobs.cleanup_old_execution_history", None, {"days": 30, "limit": 10})
        assert run_all() == ["succeeded"]
        with pytest.raises(PermissionDenied):
            js.create_manual(nw["jobs_manager"], "jobs.cleanup_old_execution_history", None, {})
        with pytest.raises(js.JobError):
            js.create_manual(nw["super"], "jobs.cleanup_old_execution_history", None, {"days": 99999})
        job.refresh_from_db()
        assert job.status == "succeeded"

