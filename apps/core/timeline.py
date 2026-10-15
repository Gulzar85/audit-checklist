"""Turn activity-log rows into the structure the timeline component renders.

``group_activity(entries)`` groups events by day (Today / Yesterday / a date), newest first, and maps
each event's verb to an icon and a semantic tone.  Any module's history (workflow events, corrective-action
history, audit trails) can feed the same component by producing the same small dicts.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.utils import timezone
from django.utils.formats import date_format

_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("created", "added", "imported", "cloned"), "success", "plus"),
    (("published", "activated", "approved", "verified", "linked", "success"), "success", "circle-check"),
    (("suspended", "deactivated", "removed", "archived", "unlinked", "left", "failure"), "warning", "circle-pause"),
    (("deleted", "rejected"), "danger", "circle-x"),
    (("rolled_back", "versioned"), "warning", "undo-2"),
    (("updated", "changed", "synced"), "info", "pencil"),
    (("switched", "logout", "preference"), "neutral", "arrow-left-right"),
)


def classify(verb: str) -> tuple[str, str]:
    """``(tone, icon)`` for an activity verb such as ``"theme.published"``."""
    tail = (verb or "").rsplit(".", 1)[-1]
    for words, tone, icon in _RULES:
        if any(word in tail for word in words):
            return tone, icon
    return "neutral", "circle"


def _day_label(day: Any, today: Any) -> str:
    if day == today:
        return "Today"
    if (today - day).days == 1:
        return "Yesterday"
    return date_format(day, "DATE_FORMAT")


def group_activity(entries: Iterable[Any]) -> list[dict[str, Any]]:
    """Group ``ActivityLog``-like rows (``verb``, ``description``, ``actor``, ``created_at``) by day."""
    today = timezone.localdate()
    days: list[dict[str, Any]] = []
    index: dict[Any, dict[str, Any]] = {}
    for entry in entries:
        moment = timezone.localtime(entry.created_at)
        day = moment.date()
        if day not in index:
            index[day] = {"day": _day_label(day, today), "events": []}
            days.append(index[day])
        tone, icon = classify(entry.verb)
        actor = getattr(entry.actor, "get_short_name", lambda: "")() if getattr(entry, "actor", None) else ""
        index[day]["events"].append(
            {
                "actor": actor,
                "text": (entry.description or entry.verb),
                "time": moment.strftime("%H:%M"),
                "datetime": moment.isoformat(),
                "tone": tone,
                "icon": icon,
            }
        )
    return days