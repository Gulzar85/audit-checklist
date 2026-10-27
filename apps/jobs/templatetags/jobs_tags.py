"""Display helpers: times in the schedule's own zone (the stored value is always UTC)."""

from __future__ import annotations

from typing import Any

from django import template

from apps.jobs import recurrence

register = template.Library()


@register.filter
def in_zone(value: Any, zone_name: str = "UTC") -> str:
    """``2026-03-08 09:00 EDT`` - a UTC instant shown on the clock of ``zone_name``."""
    if not value:
        return "-"
    try:
        local = value.astimezone(recurrence.get_zone(zone_name))
    except Exception:  # noqa: BLE001 - a bad zone never breaks a page
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return local.strftime("%Y-%m-%d %H:%M %Z")


@register.filter
def recurrence_summary(schedule: Any) -> str:
    return recurrence.describe(schedule.schedule_type, schedule.recurrence, schedule.timezone)
