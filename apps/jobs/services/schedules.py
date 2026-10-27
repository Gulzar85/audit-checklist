"""Schedule configuration: create, edit, enable / disable / pause / resume - always through here (web, API and admin share it)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from .. import constants as k
from .. import policies, recurrence, registry
from ..errors import JobConflict, JobError
from ..models import ScheduleDefinition, ScheduledJob
from . import jobs
from .base import VERBS, audit

EDITABLE = ("name", "description", "task_type", "schedule_type", "timezone", "start_at", "end_at", "recurrence", "payload", "misfire_policy", "max_attempts")
ONE_TIME_TOLERANCE_SECONDS = 60


def _aware(value: Any, label: str) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise JobError(f"{label} must be a date and time with a time zone.", "invalid_time")
    return value.astimezone(recurrence.UTC)


def clean_schedule(organization: Any, data: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Validate a complete schedule description and return the normalized field values (raises ``JobError``)."""
    task = registry.require(data.get("task_type"))
    if not task.schedulable:
        raise JobError("That task type cannot be scheduled.", "not_schedulable")
    if task.scope == registry.SCOPE_GLOBAL and organization is not None:
        raise JobError("That task is platform-wide and cannot belong to an organization.", "scope_mismatch")
    if task.scope == registry.SCOPE_ORGANIZATION and organization is None:
        raise JobError("That task needs an organization.", "scope_mismatch")
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 120:
        raise JobError("Give the schedule a name (up to 120 characters).", "invalid_name")
    description = str(data.get("description") or "")
    if len(description) > 500:
        raise JobError("The description is limited to 500 characters.", "invalid_description")
    schedule_type = data.get("schedule_type")
    zone = recurrence.get_zone(data.get("timezone") or "UTC")
    rec = recurrence.clean_recurrence(schedule_type, data.get("recurrence"))
    start_at = _aware(data.get("start_at"), "The start")
    if start_at is None:
        raise JobError("Choose when the schedule starts.", "invalid_time")
    end_at = _aware(data.get("end_at"), "The end")
    if schedule_type == k.ScheduleType.ONE_TIME:
        if end_at is not None:
            raise JobError("A one-time schedule has no end.", "invalid_time")
        if start_at <= now - timezone.timedelta(seconds=ONE_TIME_TOLERANCE_SECONDS):
            raise JobError("A one-time schedule must be in the future.", "past_time")
    if end_at is not None and end_at <= start_at:
        raise JobError("The end must be after the start.", "invalid_time")
    payload = registry.clean_payload(task, data.get("payload"))
    if len(json.dumps(payload)) > k.MAX_PAYLOAD_BYTES:
        raise JobError("The payload is too large.", "invalid_payload")
    policy = data.get("misfire_policy") or k.MisfirePolicy.SKIP
    if policy not in k.MisfirePolicy.values:
        raise JobError("Choose how missed runs are handled.", "invalid_misfire")
    attempts = data.get("max_attempts")
    if attempts is not None and (isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= k.MAX_ATTEMPTS_LIMIT):
        raise JobError(f"The maximum attempts must be from 1 to {k.MAX_ATTEMPTS_LIMIT}.", "invalid_attempts")
    return {"name": name, "description": description, "task_type": task.code, "schedule_type": schedule_type, "timezone": zone.key, "start_at": start_at, "end_at": end_at,
            "recurrence": rec, "payload": payload, "misfire_policy": policy, "max_attempts": registry.retry_limits(task, attempts)}


def _first_run(values: dict[str, Any], now: datetime) -> datetime | None:
    return recurrence.next_after(values["schedule_type"], values["recurrence"], recurrence.get_zone(values["timezone"]), values["start_at"], values["end_at"], now)


def save_schedule(actor: Any, organization: Any, data: dict[str, Any], *, schedule: ScheduleDefinition | None = None, allow_timezone_change: bool = False,
                  now: datetime | None = None) -> ScheduleDefinition:
    """Create (``schedule`` is ``None``) or edit a schedule.  Editing recomputes the next run from *now*: no run is duplicated and none is back-filled."""
    now = now or timezone.now()
    task = registry.require(data.get("task_type") if schedule is None else (data.get("task_type") or schedule.task_type))
    if schedule is not None:
        organization = schedule.organization
    if not policies.can_schedule(actor, task, organization):
        raise PermissionDenied
    merged = dict(data)
    if schedule is not None:
        merged = {**{f: getattr(schedule, f) for f in EDITABLE}, **data}
        if schedule.status == k.ScheduleStatus.FINISHED and merged.get("schedule_type") == k.ScheduleType.ONE_TIME and merged["start_at"] == schedule.start_at:
            raise JobConflict("That one-time schedule already ran. Create a new one.", "schedule_finished")
    values = clean_schedule(organization, merged, now=now)
    try:
        with transaction.atomic():
            if schedule is None:
                schedule = ScheduleDefinition(organization=organization, created_by=actor, updated_by=actor, status=k.ScheduleStatus.ACTIVE, **values)
                schedule.next_run_at = _first_run(values, now)
                if schedule.next_run_at is None:
                    raise JobError("The schedule would never run: its end is before its first run.", "never_runs")
                schedule.save()
                audit(VERBS["schedule_created"], organization, actor, schedule, f"Schedule {schedule.name} created", task_type=schedule.task_type, schedule_type=schedule.schedule_type)
            else:
                schedule = ScheduleDefinition.objects.select_for_update().get(pk=schedule.pk)
                if values["timezone"] != schedule.timezone and not allow_timezone_change:
                    raise JobError("Changing the time zone changes when this schedule runs. Confirm the change explicitly.", "timezone_change_unconfirmed")
                changed = [f for f in EDITABLE if getattr(schedule, f) != values[f]]
                for field in changed:
                    setattr(schedule, field, values[field])
                schedule.updated_by = actor
                schedule.revision += 1
                if schedule.status in (k.ScheduleStatus.ACTIVE, k.ScheduleStatus.FINISHED):
                    schedule.next_run_at = _first_run(values, now)
                    schedule.status = k.ScheduleStatus.ACTIVE if schedule.next_run_at else k.ScheduleStatus.FINISHED
                schedule.save()
                audit(VERBS["schedule_updated"], organization, actor, schedule, f"Schedule {schedule.name} updated", changed=changed, revision=schedule.revision)
    except IntegrityError as exc:
        raise JobError("A schedule with that name already exists.", "duplicate_name") from exc
    return schedule


_ACTIONS = {
    "enable": ({k.ScheduleStatus.DISABLED, k.ScheduleStatus.FINISHED}, k.ScheduleStatus.ACTIVE, "schedule_enabled"),
    "disable": ({k.ScheduleStatus.ACTIVE, k.ScheduleStatus.PAUSED}, k.ScheduleStatus.DISABLED, "schedule_disabled"),
    "pause": ({k.ScheduleStatus.ACTIVE}, k.ScheduleStatus.PAUSED, "schedule_paused"),
    "resume": ({k.ScheduleStatus.PAUSED}, k.ScheduleStatus.ACTIVE, "schedule_resumed"),
}


def set_status(schedule: ScheduleDefinition, actor: Any, action: str, *, now: datetime | None = None) -> ScheduleDefinition:
    """enable / disable / pause / resume.  Becoming active computes the next run from *now* (no back-fill, so no duplicate run); leaving *active* skips the occurrences that have not started."""
    now = now or timezone.now()
    if action not in _ACTIONS:
        raise JobError("Unknown schedule action.", "unknown_action")
    task = registry.require(schedule.task_type)
    if not policies.can_schedule(actor, task, schedule.organization):
        raise PermissionDenied
    allowed_from, target, verb = _ACTIONS[action]
    with transaction.atomic():
        row = ScheduleDefinition.objects.select_for_update().get(pk=schedule.pk)
        if row.status not in allowed_from:
            raise JobConflict(f"A {row.get_status_display().lower()} schedule cannot be {action}d.", "invalid_transition")
        row.status = target
        if target == k.ScheduleStatus.ACTIVE:
            row.next_run_at = _first_run({"schedule_type": row.schedule_type, "recurrence": row.recurrence, "timezone": row.timezone, "start_at": row.start_at, "end_at": row.end_at}, now)
            if row.next_run_at is None:
                raise JobConflict("That schedule has no further runs. Edit it first.", "never_runs")
        else:
            row.next_run_at = None
            ScheduledJob.objects.filter(schedule=row, trigger=k.Trigger.SCHEDULE, status__in=[k.JobStatus.PENDING, k.JobStatus.RETRY_PENDING]).update(
                status=k.JobStatus.SKIPPED, finished_at=now, error_category="schedule_inactive", error_message="The schedule was stopped before this run started.")
        row.updated_by = actor
        row.revision += 1
        row.save()
        audit(VERBS[verb], row.organization, actor, row, f"Schedule {row.name} {target}", revision=row.revision)
    return row


def run_now(schedule: ScheduleDefinition, actor: Any, *, client_key: str = "") -> ScheduledJob:
    """Queue one extra run of the schedule's task and payload (the schedule itself is untouched).  ``client_key`` makes a retried request a no-op."""
    return jobs.create_manual(actor, schedule.task_type, schedule.organization, schedule.payload, schedule=schedule, client_key=client_key)
