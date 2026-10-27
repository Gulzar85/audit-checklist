"""Phase 18 core: recurrence and time zones, schedule validation, the job lifecycle, retries, leases, concurrency and permissions."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone as tz
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.utils import timezone

from apps.jobs import constants as k
from apps.jobs import recurrence, registry, selectors, services as js
from apps.jobs.errors import JobConflict, JobError, PermanentError, RetryableError
from apps.jobs.models import JobExecution, ScheduleDefinition, ScheduledJob

from jobs_support import CALLS, build_jobs_world, reset

UTC = tz.utc


@pytest.fixture
def w(org_a, org_b, make_user, assign, superuser):
    return build_jobs_world(org_a, org_b, make_user, assign, superuser)


@pytest.fixture(autouse=True)
def _no_jitter(settings):
    settings.JOBS_RETRY_JITTER = 0


def dt(*a):
    return datetime(*a, tzinfo=UTC)


def sched(w, **over):
    data = {"name": "S1", "task_type": "test.echo", "schedule_type": "interval", "timezone": "UTC", "start_at": dt(2026, 1, 1), "recurrence": {"every_minutes": 15}, "payload": {"n": 2}}
    data.update(over)
    return js.save_schedule(w["manager"], w["org_a"], data, now=over.pop("_now", dt(2026, 1, 1, 0, 1)))


# ------------------------------------------------------------------------------------------------------------------ recurrence
class TestRecurrence:
    def nxt(self, stype, rec, zone, start, after, end=None):
        return recurrence.next_after(stype, rec, ZoneInfo(zone), start, end, after)

    def test_interval_is_anchored_on_the_start(self):
        assert self.nxt("interval", {"every_minutes": 15}, "UTC", dt(2026, 1, 1), dt(2026, 1, 1, 0, 14)) == dt(2026, 1, 1, 0, 15)
        assert self.nxt("interval", {"every_minutes": 15}, "UTC", dt(2026, 1, 1), dt(2026, 1, 1, 0, 15)) == dt(2026, 1, 1, 0, 30)  # strictly after
        assert self.nxt("interval", {"every_minutes": 15}, "UTC", dt(2026, 1, 1, 6), dt(2026, 1, 1)) == dt(2026, 1, 1, 6)  # not before the start

    def test_daily_weekly_and_monthly(self):
        assert self.nxt("daily", {"time": "09:00"}, "Asia/Karachi", dt(2026, 1, 1), dt(2026, 1, 1, 5)) == dt(2026, 1, 2, 4)  # 09:00 PKT = 04:00 UTC
        weekly = {"time": "08:00", "weekdays": [0, 3]}  # Monday, Thursday
        assert self.nxt("weekly", weekly, "UTC", dt(2026, 1, 1), dt(2026, 1, 1, 9)) == dt(2026, 1, 5, 8)  # 2026-01-01 is a Thursday; 08:00 has passed
        assert self.nxt("weekly", weekly, "UTC", dt(2026, 1, 1), dt(2026, 1, 5, 8)) == dt(2026, 1, 8, 8)
        assert self.nxt("monthly", {"time": "06:00", "day": 31}, "UTC", dt(2026, 1, 1), dt(2026, 2, 1)) == dt(2026, 2, 28, 6)  # February has no 31st: its last day
        assert self.nxt("monthly", {"time": "06:00", "day": 31}, "UTC", dt(2026, 1, 1), dt(2026, 2, 28, 6)) == dt(2026, 3, 31, 6)

    def test_daylight_saving_follows_the_local_clock(self):
        # New York: clocks go forward on 2026-03-08 (02:00 -> 03:00) and back on 2026-11-01 (02:00 -> 01:00).
        assert self.nxt("daily", {"time": "09:00"}, "America/New_York", dt(2026, 3, 1), dt(2026, 3, 7, 15)) == dt(2026, 3, 8, 13)  # 09:00 EDT = 13:00 UTC (was 14:00 UTC in winter)
        assert self.nxt("daily", {"time": "09:00"}, "America/New_York", dt(2026, 3, 1), dt(2026, 3, 1, 15)) == dt(2026, 3, 2, 14)

    def test_a_nonexistent_local_time_runs_shifted_forward_and_an_ambiguous_one_once(self):
        gap = self.nxt("daily", {"time": "02:30"}, "America/New_York", dt(2026, 3, 1), dt(2026, 3, 8, 0))
        assert gap == dt(2026, 3, 8, 7, 30)  # 02:30 does not exist: it runs at 03:30 EDT (07:30 UTC), the same instant the clock reaches that point
        first = self.nxt("daily", {"time": "01:30"}, "America/New_York", dt(2026, 10, 1), dt(2026, 11, 1, 0))
        assert first == dt(2026, 11, 1, 5, 30)  # 01:30 happens twice: the first (EDT, 05:30 UTC)
        after_first = self.nxt("daily", {"time": "01:30"}, "America/New_York", dt(2026, 10, 1), first)
        assert after_first == dt(2026, 11, 2, 6, 30)  # the second 01:30 (EST) is skipped: once a day

    def test_end_bounds_and_one_time(self):
        assert self.nxt("interval", {"every_minutes": 60}, "UTC", dt(2026, 1, 1), dt(2026, 1, 1, 1, 30), end=dt(2026, 1, 1, 2)) == dt(2026, 1, 1, 2)
        assert self.nxt("interval", {"every_minutes": 60}, "UTC", dt(2026, 1, 1), dt(2026, 1, 1, 2), end=dt(2026, 1, 1, 2)) is None
        assert self.nxt("one_time", {}, "UTC", dt(2026, 1, 1, 5), dt(2026, 1, 1, 4)) == dt(2026, 1, 1, 5)
        assert self.nxt("one_time", {}, "UTC", dt(2026, 1, 1, 5), dt(2026, 1, 1, 5)) is None

    @pytest.mark.parametrize("stype,rec", [("interval", {"every_minutes": 1}), ("interval", {"every_minutes": "5"}), ("interval", {"every_minutes": True}), ("interval", {}), ("daily", {"time": "9:00"}),
                                           ("daily", {"time": "25:00"}), ("daily", {"time": "09:00", "x": 1}), ("weekly", {"time": "09:00", "weekdays": []}), ("weekly", {"time": "09:00", "weekdays": [7]}),
                                           ("monthly", {"time": "09:00", "day": 0}), ("monthly", {"time": "09:00", "day": 32}), ("cron", {"expr": "* * * * *"}), ("one_time", {"x": 1})])
    def test_invalid_recurrence_is_refused(self, stype, rec):
        with pytest.raises(JobError):
            recurrence.clean_recurrence(stype, rec)

    @pytest.mark.parametrize("name", ["Mars/Base", "", "x" * 80, None, 5, "../../etc/passwd"])
    def test_invalid_time_zones_are_refused(self, name):
        with pytest.raises(JobError):
            recurrence.get_zone(name)


# ------------------------------------------------------------------------------------------------------------------ schedules
@pytest.mark.django_db
class TestSchedules:
    def test_create_edit_and_audit(self, w):
        s = sched(w)
        assert s.next_run_at == dt(2026, 1, 1, 0, 15) and s.status == "active" and s.revision == 1 and s.max_attempts == 3
        s2 = js.save_schedule(w["manager"], None, {"name": "S1 renamed"}, schedule=s, now=dt(2026, 1, 1, 0, 20))
        assert s2.name == "S1 renamed" and s2.revision == 2 and s2.next_run_at == dt(2026, 1, 1, 0, 30)
        from apps.core.models import ActivityLog
        assert ActivityLog.objects.filter(verb__in=["jobs.schedule_created", "jobs.schedule_updated"]).count() == 2

    @pytest.mark.parametrize("over,code", [
        ({"task_type": "os.system"}, "unknown_task"), ({"payload": {"n": 99}}, "invalid_payload"),
        ({"payload": {"evil": 1}}, "invalid_payload"), ({"payload": {"n": True}}, "invalid_payload"), ({"timezone": "Nope/Zone"}, "invalid_timezone"),
        ({"recurrence": {"every_minutes": 1}}, "invalid_recurrence"), ({"schedule_type": "cron"}, "invalid_schedule_type"), ({"name": ""}, "invalid_name"),
        ({"end_at": dt(2025, 1, 1)}, "invalid_time"), ({"start_at": None}, "invalid_time"), ({"start_at": datetime(2026, 1, 1)}, "invalid_time"),
        ({"misfire_policy": "bogus"}, "invalid_misfire"), ({"max_attempts": 99}, "invalid_attempts"), ({"max_attempts": 0}, "invalid_attempts"),
        ({"schedule_type": "one_time", "recurrence": {}, "start_at": dt(2025, 1, 1)}, "past_time"),
    ])
    def test_invalid_configurations_are_refused(self, w, over, code):
        with pytest.raises(JobError) as info:
            sched(w, **over)
        assert info.value.code == code

    def test_attempts_are_capped_by_the_task(self, w):
        with pytest.raises(JobError):
            sched(w, max_attempts=11)
        assert sched(w, max_attempts=10).max_attempts == 3  # test.echo allows 3

    def test_changing_the_time_zone_must_be_confirmed(self, w):
        s = sched(w, schedule_type="daily", recurrence={"time": "09:00"})
        with pytest.raises(JobError) as info:
            js.save_schedule(w["manager"], None, {"timezone": "Asia/Karachi"}, schedule=s)
        assert info.value.code == "timezone_change_unconfirmed"
        assert js.save_schedule(w["manager"], None, {"timezone": "Asia/Karachi"}, schedule=s, allow_timezone_change=True).timezone == "Asia/Karachi"

    def test_pause_resume_does_not_backfill_and_disable_skips_unstarted_jobs(self, w):
        s = sched(w)
        s = js.set_status(s, w["manager"], "pause", now=dt(2026, 1, 1, 0, 5))
        assert s.status == "paused" and s.next_run_at is None
        with pytest.raises(JobConflict):
            js.set_status(s, w["manager"], "pause")
        s = js.set_status(s, w["manager"], "resume", now=dt(2026, 1, 1, 5, 7))
        assert s.next_run_at == dt(2026, 1, 1, 5, 15)  # from "now": the five paused hours are not replayed
        js.materialize_due(dt(2026, 1, 1, 5, 16))
        job = ScheduledJob.objects.get(schedule=s)
        js.set_status(s, w["manager"], "disable", now=dt(2026, 1, 1, 5, 17))
        job.refresh_from_db()
        assert job.status == "skipped" and job.error_category == "schedule_inactive"

    def test_names_are_unique_per_organization_and_global_schedules_need_a_superuser(self, w):
        sched(w)
        with pytest.raises(JobError) as info:
            sched(w)
        assert info.value.code == "duplicate_name"
        other = js.save_schedule(w["other"], w["org_b"], {"name": "S1", "task_type": "test.echo", "schedule_type": "daily", "timezone": "UTC", "start_at": dt(2026, 1, 1), "recurrence": {"time": "01:00"}})
        assert other.organization == w["org_b"]  # the same name in another organization is fine
        data = {"name": "G", "task_type": "test.global_only", "schedule_type": "daily", "timezone": "UTC", "start_at": dt(2026, 1, 1), "recurrence": {"time": "01:00"}}
        with pytest.raises(PermissionDenied):
            js.save_schedule(w["manager"], None, data)
        assert js.save_schedule(w["super"], None, data).organization is None


# ------------------------------------------------------------------------------------------------------------------ materialization
@pytest.mark.django_db
class TestMaterialization:
    def test_a_due_occurrence_becomes_exactly_one_job(self, w):
        s = sched(w)
        assert js.materialize_due(dt(2026, 1, 1, 0, 16))["created"] == 1
        assert js.materialize_due(dt(2026, 1, 1, 0, 16))["created"] == 0  # already advanced
        s.refresh_from_db()
        assert s.next_run_at == dt(2026, 1, 1, 0, 30) and s.last_run_at == dt(2026, 1, 1, 0, 16)
        job = ScheduledJob.objects.get()
        assert (job.organization, job.status, job.trigger, job.scheduled_for, job.payload) == (w["org_a"], "pending", "schedule", dt(2026, 1, 1, 0, 15), {"n": 2})

    def test_the_same_occurrence_can_never_be_created_twice(self, w):
        s = sched(w)
        key = f"sched:{s.pk}:{dt(2026, 1, 1, 0, 15).isoformat()}"
        a, made_a = js.enqueue("test.echo", organization=w["org_a"], payload={}, schedule=s, idempotency_key=key)
        b, made_b = js.enqueue("test.echo", organization=w["org_a"], payload={}, schedule=s, idempotency_key=key)
        assert a.pk == b.pk and made_a and not made_b

    def test_misfire_skip_records_one_skipped_job_and_jumps_to_the_future(self, w):
        s = sched(w, misfire_policy="skip")
        js.materialize_due(dt(2026, 1, 1, 6, 1))  # six hours of downtime
        job = ScheduledJob.objects.get()
        assert job.status == "skipped" and job.error_category == "misfire"
        s.refresh_from_db()
        assert s.next_run_at == dt(2026, 1, 1, 6, 15)

    def test_misfire_run_once_catches_up_once(self, w):
        s = sched(w, misfire_policy="run_once")
        js.materialize_due(dt(2026, 1, 1, 6, 1))
        job = ScheduledJob.objects.get()
        assert job.status == "pending" and job.scheduled_for == dt(2026, 1, 1, 0, 15)
        s.refresh_from_db()
        assert s.next_run_at == dt(2026, 1, 1, 6, 15)

    def test_a_schedule_never_overlaps_itself(self, w):
        sched(w)
        js.materialize_due(dt(2026, 1, 1, 0, 16))
        js.materialize_due(dt(2026, 1, 1, 0, 31))  # the first job is still pending
        assert sorted(ScheduledJob.objects.values_list("status", flat=True)) == ["pending", "skipped"]

    def test_one_time_schedules_finish_and_disabled_or_paused_ones_do_not_run(self, w):
        once = sched(w, name="once", schedule_type="one_time", recurrence={}, start_at=dt(2026, 1, 1, 1), _now=dt(2026, 1, 1))
        off = sched(w, name="off")
        js.set_status(off, w["manager"], "disable")
        js.materialize_due(dt(2026, 1, 1, 1, 0, 30))
        once.refresh_from_db()
        assert once.status == "finished" and once.next_run_at is None and ScheduledJob.objects.filter(schedule=once).count() == 1
        assert not ScheduledJob.objects.filter(schedule=off).exists()
        js.materialize_due(dt(2026, 1, 2))
        assert ScheduledJob.objects.filter(schedule=once).count() == 1

    def test_the_batch_is_bounded(self, w):
        for i in range(5):
            sched(w, name=f"S{i}")
        assert js.materialize_due(dt(2026, 1, 1, 0, 16), limit=2)["created"] == 2
        assert js.materialize_due(dt(2026, 1, 1, 0, 16), limit=10)["created"] == 3


# ------------------------------------------------------------------------------------------------------------------ execution
def make_job(w, **kw):
    job, _ = js.enqueue("test.echo", organization=w["org_a"], payload=kw.pop("payload", {"n": 1}), **kw)
    return job


def run(job_pk=None, now=None):
    claimed = js.claim(10, runner="t", now=now or timezone.now(), job_id=job_pk)
    return [js.execute(pk, token, now=(lambda: now) if now else timezone.now) for pk, token in claimed]


@pytest.mark.django_db
class TestExecution:
    def test_success_records_history_and_a_bounded_result(self, w):
        reset({"ok": 1, "nested": {"x": 1}})
        job = make_job(w)
        assert run() == ["succeeded"]
        job.refresh_from_db()
        assert job.status == "succeeded" and job.attempt_count == 1 and job.result == {"ok": 1} and job.lease_token == "" and job.finished_at and job.duration_ms is not None
        ex = job.executions.get()
        assert (ex.number, ex.outcome, ex.correlation_id) == (1, "succeeded", job.correlation_id)
        assert CALLS[0]["org"] == w["org_a"]  # the organization comes from the job row

    def test_retryable_failures_back_off_exponentially_then_exhaust(self, w):
        reset(RetryableError("provider down"), RetryableError("again"), RetryableError("still"))
        job = make_job(w)
        t0 = timezone.now()
        run(now=t0)
        job.refresh_from_db()
        assert job.status == "retry_pending" and job.available_at == t0 + timedelta(seconds=10) and job.attempt_count == 1
        assert run(now=t0 + timedelta(seconds=5)) == []  # not before the retry time
        run(now=t0 + timedelta(seconds=11))
        job.refresh_from_db()
        assert job.status == "retry_pending" and job.available_at == t0 + timedelta(seconds=11 + 20)  # 10 * 2^1
        run(now=t0 + timedelta(seconds=40))
        job.refresh_from_db()
        assert job.status == "failed" and job.attempt_count == 3 and [e.outcome for e in job.executions.all()] == ["retryable_failure", "retryable_failure", "retries_exhausted"]
        assert run(now=t0 + timedelta(days=1)) == []  # no infinite loop

    def test_the_delay_is_capped(self, w):
        task = registry.get("test.echo")
        from apps.jobs.services import runner
        assert runner.backoff_seconds(task, 1) == 10 and runner.backoff_seconds(task, 5) == 100 and runner.backoff_seconds(task, 20) == 100

    def test_permanent_and_unexpected_failures_are_not_retried_and_leak_nothing(self, w):
        reset(PermanentError("bad configuration"), ValueError("secret-password-123 in a traceback"))
        first, second = make_job(w), make_job(w)
        run()
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.status == "failed" and first.attempt_count == 1 and first.error_message == "bad configuration"
        assert second.status == "failed" and second.error_message == "Unexpected ValueError." and "secret" not in second.error_message and second.error_category == "unexpected_error"

    def test_infrastructure_errors_are_retried_without_storing_their_text(self, w):
        reset(OSError("connect to db.internal:5432 refused"))
        job = make_job(w)
        run()
        job.refresh_from_db()
        assert job.status == "retry_pending" and "db.internal" not in job.error_message

    def test_unknown_task_type_fails_without_retry(self, w):
        job = make_job(w)
        ScheduledJob.objects.filter(pk=job.pk).update(task_type="removed.task")
        run()
        job.refresh_from_db()
        assert job.status == "failed" and job.error_category == "unknown_task" and job.attempt_count == 1

    def test_a_cancelled_job_never_starts_and_a_running_job_cannot_be_cancelled(self, w):
        job = make_job(w)
        js.cancel(job, w["manager"], "not needed")
        assert job.status == "cancelled" and run() == []
        running = make_job(w)
        pk, token = js.claim(1, job_id=running.pk)[0]
        js.runner._begin(pk, token, timezone.now())
        with pytest.raises(JobConflict):
            js.cancel(running, w["manager"], "too late")
        claimed = make_job(w)
        js.claim(1, job_id=claimed.pk)
        assert js.cancel(claimed, w["manager"], "before it started").status == "cancelled"  # claimed but not started: cancellable, and begin() will refuse it

    def test_manual_retry_of_a_failed_job_only(self, w):
        reset(PermanentError("no"))
        job = make_job(w)
        run()
        job.refresh_from_db()
        assert job.status == "failed"
        with pytest.raises(JobError):
            js.retry(job, w["manager"], "")
        js.retry(job, w["manager"], "fixed the configuration")
        assert (job.status, job.attempt_count) == ("pending", 0)
        assert run() == ["succeeded"] and job.executions.count() == 2  # history keeps both attempts
        with pytest.raises(JobConflict):
            js.retry(job, w["manager"], "again please")

    def test_expired_leases_are_recovered_and_a_stale_runner_cannot_overwrite(self, w):
        job = make_job(w)
        t0 = timezone.now()
        pk, token = js.claim(1, runner="dead", now=t0)[0]
        js.runner._begin(pk, token, t0)  # started, then the runner died
        assert js.recover_expired(t0 + timedelta(seconds=30)) == 0  # lease not expired yet
        assert js.recover_expired(t0 + timedelta(hours=1)) == 1
        job.refresh_from_db()
        assert job.status == "retry_pending" and job.lease_token == "" and job.executions.get().outcome == "lease_expired"
        # the dead runner wakes up and tries to finish with its old token: nothing changes
        assert js.runner._finish(job, token, outcome="succeeded", started=t0, now=t0, duration_ms=1, task=registry.get("test.echo"), result={"x": 1}) == "lost"
        job.refresh_from_db()
        assert job.status == "retry_pending"

    def test_a_claimed_job_that_never_started_goes_back_to_the_queue(self, w):
        job = make_job(w)
        t0 = timezone.now()
        js.claim(1, now=t0)
        js.recover_expired(t0 + timedelta(hours=1))
        job.refresh_from_db()
        assert job.status == "pending" and job.attempt_count == 0 and not job.executions.exists()

    def test_exhausted_lease_recoveries_fail_the_job(self, w):
        job = make_job(w, max_attempts=1)
        t0 = timezone.now()
        pk, token = js.claim(1, now=t0)[0]
        js.runner._begin(pk, token, t0)
        js.recover_expired(t0 + timedelta(hours=1))
        job.refresh_from_db()
        assert job.status == "failed"

    def test_the_run_batch_is_bounded_and_ordered_by_priority(self, w):
        for i in range(5):
            make_job(w, priority=i)
        claimed = js.claim(3, runner="t")
        assert len(claimed) == 3
        assert [ScheduledJob.objects.get(pk=pk).priority for pk, _ in claimed] == [4, 3, 2]
        assert ScheduledJob.objects.filter(status="pending").count() == 2

    def test_run_once_recovers_materializes_runs_and_records_a_heartbeat(self, w):
        sched(w)
        s = js.run_once(limit=5, runner="r1", now=lambda: dt(2026, 1, 1, 0, 16))
        assert s["materialized"]["created"] == 1 and s["succeeded"] == 1 and s["failed"] == 0
        from apps.jobs.models import RunnerHeartbeat
        assert RunnerHeartbeat.objects.get(runner="r1").jobs_processed == 1

    def test_a_stop_request_releases_unstarted_jobs(self, w):
        for _ in range(3):
            make_job(w)
        s = js.run_once(limit=3, runner="r1", stop=lambda: True)
        assert s["ran"] == 0 and s["released"] == 3 and ScheduledJob.objects.filter(status="pending").count() == 3


# ------------------------------------------------------------------------------------------------------------------ concurrency
@pytest.mark.django_db(transaction=True)
class TestConcurrency:
    def test_concurrent_runners_never_claim_the_same_job(self, w):
        for _ in range(12):
            make_job(w)
        results, barrier = [], threading.Barrier(4)

        def worker(name):
            try:
                barrier.wait()
                results.append([pk for pk, _ in js.claim(6, runner=name)])
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        flat = [pk for part in results for pk in part]
        assert len(flat) == len(set(flat)) == 12

    def test_concurrent_materialization_creates_one_job(self, w):
        sched(w)
        barrier = threading.Barrier(4)

        def worker():
            try:
                barrier.wait()
                js.materialize_due(dt(2026, 1, 1, 0, 16))
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert ScheduledJob.objects.count() == 1


# ------------------------------------------------------------------------------------------------------------------ permissions
@pytest.mark.django_db
class TestPermissions:
    def test_manual_runs_need_the_permission_and_the_right_scope(self, w):
        assert js.create_manual(w["runner_only"], "test.echo", w["org_a"], {"n": 3}).status == "pending"
        for user in (w["viewer"], w["nobody"], w["other"]):
            with pytest.raises(PermissionDenied):
                js.create_manual(user, "test.echo", w["org_a"], {})
        with pytest.raises(PermissionDenied):
            js.create_manual(w["manager"], "test.global_only", None, {})  # platform-wide: superusers only
        with pytest.raises(PermissionDenied):
            js.create_manual(w["manager"], "test.org_only", None, {})
        assert js.create_manual(w["super"], "test.global_only", None, {}).organization is None
        with pytest.raises(JobError):
            js.create_manual(w["manager"], "os.system", w["org_a"], {})

    def test_a_repeated_client_key_returns_the_first_job(self, w):
        a = js.create_manual(w["manager"], "test.echo", w["org_a"], {}, client_key="abc")
        b = js.create_manual(w["manager"], "test.echo", w["org_a"], {}, client_key="abc")
        assert a.pk == b.pk and ScheduledJob.objects.count() == 1

    def test_delay_is_bounded(self, w):
        for bad in (-1, k.MAX_DELAY_SECONDS + 1, True, "5"):
            with pytest.raises(JobError):
                js.create_manual(w["manager"], "test.echo", w["org_a"], {}, delay_seconds=bad)
        job = js.create_manual(w["manager"], "test.echo", w["org_a"], {}, delay_seconds=3600)
        assert job.available_at > timezone.now() + timedelta(minutes=59) and run() == []

    def test_cancel_and_retry_need_the_run_permission_and_a_reason(self, w):
        job = make_job(w)
        for user in (w["viewer"], w["other"], w["nobody"]):
            with pytest.raises(PermissionDenied):
                js.cancel(job, user, "reason here")
        with pytest.raises(JobError):
            js.cancel(job, w["manager"], "")

    def test_visibility_is_scoped_to_the_organization(self, w):
        mine = make_job(w)
        theirs, _ = js.enqueue("test.echo", organization=w["org_b"])
        glob, _ = js.enqueue("test.global_only")
        assert set(selectors.jobs_visible_to(w["viewer"])) == {mine}
        assert set(selectors.jobs_visible_to(w["other"])) == {theirs}
        assert set(selectors.jobs_visible_to(w["super"])) == {mine, theirs, glob}
        assert not selectors.jobs_visible_to(w["nobody"]).exists()
        assert selectors.get_job(w["other"], mine.pk) is None

    def test_retention_keeps_open_jobs_and_recent_history(self, w):
        old = make_job(w)
        ScheduledJob.objects.filter(pk=old.pk).update(status="succeeded", finished_at=timezone.now() - timedelta(days=200))
        recent = make_job(w)
        ScheduledJob.objects.filter(pk=recent.pk).update(status="succeeded", finished_at=timezone.now() - timedelta(days=1))
        open_job = make_job(w)
        ScheduledJob.objects.filter(pk=open_job.pk).update(created_at=timezone.now() - timedelta(days=400))
        assert js.cleanup(days=90)["rows_deleted"] == 1
        assert set(ScheduledJob.objects.values_list("pk", flat=True)) == {recent.pk, open_job.pk}
        with pytest.raises(JobError):
            js.cleanup(days=3)
