"""Recurrence arithmetic (Phase 18): when does a schedule run next?  No cron parser: five structured, validated shapes only.

* ``one_time``  - ``start_at`` (a UTC instant).
* ``interval``  - ``{"every_minutes": N}`` counted from ``start_at`` (pure UTC arithmetic; daylight saving cannot affect it).
* ``daily``     - ``{"time": "HH:MM"}`` in the schedule's time zone.
* ``weekly``    - ``{"time": "HH:MM", "weekdays": [0..6]}`` (Monday = 0).
* ``monthly``   - ``{"time": "HH:MM", "day": 1..31}``; a day the month does not have means the month's last day.

Wall-clock schedules follow the *local* clock.  A local time that does not exist (the spring-forward gap) runs at the same instant the clock jumps to, i.e. shifted
forward by the gap; an ambiguous time (the autumn overlap) runs at its first occurrence, once.  Timestamps are stored in UTC and the zone is stored explicitly.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import constants as k
from .errors import JobError

UTC = dt_timezone.utc
HORIZON_DAYS = 400


def get_zone(name: Any) -> ZoneInfo:
    if not isinstance(name, str) or not name or len(name) > 64:
        raise JobError("Choose a valid time zone.", "invalid_timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise JobError("Choose a valid time zone.", "invalid_timezone") from exc


def _parse_time(value: Any) -> time:
    try:
        hour, minute = str(value).split(":")
        if len(hour) != 2 or len(minute) != 2:
            raise ValueError
        return time(int(hour), int(minute))
    except (ValueError, TypeError) as exc:
        raise JobError("The time must be HH:MM (24 hour clock).", "invalid_recurrence") from exc


def clean_recurrence(schedule_type: str, raw: Any) -> dict[str, Any]:
    """Validate and normalize the recurrence settings of ``schedule_type`` (unknown keys are errors)."""
    raw = raw if isinstance(raw, dict) else {}
    allowed = {k.ScheduleType.ONE_TIME: set(), k.ScheduleType.INTERVAL: {"every_minutes"}, k.ScheduleType.DAILY: {"time"}, k.ScheduleType.WEEKLY: {"time", "weekdays"},
               k.ScheduleType.MONTHLY: {"time", "day"}}.get(schedule_type)
    if allowed is None:
        raise JobError("Choose a schedule type.", "invalid_schedule_type")
    extra = sorted(set(raw) - allowed)
    if extra:
        raise JobError(f"Unknown recurrence setting(s): {', '.join(map(str, extra))[:80]}.", "invalid_recurrence")
    if schedule_type == k.ScheduleType.ONE_TIME:
        return {}
    if schedule_type == k.ScheduleType.INTERVAL:
        every = raw.get("every_minutes")
        if isinstance(every, bool) or not isinstance(every, int) or not k.MIN_INTERVAL_MINUTES <= every <= k.MAX_INTERVAL_MINUTES:
            raise JobError(f"The interval must be a whole number of minutes from {k.MIN_INTERVAL_MINUTES} to {k.MAX_INTERVAL_MINUTES}.", "invalid_recurrence")
        return {"every_minutes": every}
    out: dict[str, Any] = {"time": _parse_time(raw.get("time")).strftime("%H:%M")}
    if schedule_type == k.ScheduleType.WEEKLY:
        days = raw.get("weekdays")
        if not isinstance(days, list) or not days or len(days) > 7 or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days):
            raise JobError("Choose at least one weekday (0 = Monday ... 6 = Sunday).", "invalid_recurrence")
        out["weekdays"] = sorted(set(days))
    if schedule_type == k.ScheduleType.MONTHLY:
        day = raw.get("day")
        if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31:
            raise JobError("The day of the month must be from 1 to 31.", "invalid_recurrence")
        out["day"] = day
    return out


def local_to_utc(day: date, at: time, zone: ZoneInfo) -> datetime:
    """The UTC instant of ``day at`` in ``zone``: first occurrence when ambiguous, shifted forward when the local time does not exist."""
    return datetime.combine(day, at).replace(tzinfo=zone, fold=0).astimezone(UTC)


def _last_day(year: int, month: int) -> int:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return (nxt - timedelta(days=1)).day


def next_after(schedule_type: str, recurrence: dict[str, Any], zone: ZoneInfo, start_at: datetime, end_at: datetime | None, after: datetime) -> datetime | None:
    """The first run strictly after ``after`` (and not before ``start_at`` nor after ``end_at``), or ``None`` when the schedule has no more runs."""
    if schedule_type == k.ScheduleType.ONE_TIME:
        found: datetime | None = start_at if start_at > after else None
    elif schedule_type == k.ScheduleType.INTERVAL:
        step = timedelta(minutes=int(recurrence["every_minutes"]))
        if after < start_at:
            found = start_at
        else:
            found = start_at + step * ((after - start_at) // step + 1)
    else:
        found = None
        at = _parse_time(recurrence["time"])
        local_after = max(after, start_at - timedelta(microseconds=1)).astimezone(zone)
        for offset in range(-1, HORIZON_DAYS):
            day = local_after.date() + timedelta(days=offset)
            if schedule_type == k.ScheduleType.WEEKLY and day.weekday() not in recurrence["weekdays"]:
                continue
            if schedule_type == k.ScheduleType.MONTHLY and day.day != min(int(recurrence["day"]), _last_day(day.year, day.month)):
                continue
            candidate = local_to_utc(day, at, zone)
            if candidate > after and candidate >= start_at:
                found = candidate
                break
    if found is not None and end_at is not None and found > end_at:
        return None
    return found


def describe(schedule_type: str, recurrence: dict[str, Any], zone_name: str) -> str:
    """A short human-readable summary."""
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if schedule_type == k.ScheduleType.ONE_TIME:
        return "Once"
    if schedule_type == k.ScheduleType.INTERVAL:
        n = recurrence.get("every_minutes", 0)
        return f"Every {n // 60} h" if n % 60 == 0 and n >= 60 else f"Every {n} min"
    at = f"{recurrence.get('time', '')} ({zone_name})"
    if schedule_type == k.ScheduleType.DAILY:
        return f"Daily at {at}"
    if schedule_type == k.ScheduleType.WEEKLY:
        return f"Weekly on {', '.join(names[d] for d in recurrence.get('weekdays', []))} at {at}"
    return f"Monthly on day {recurrence.get('day')} at {at}"
