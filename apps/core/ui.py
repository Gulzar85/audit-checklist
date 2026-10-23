"""UI vocabulary shared by templates: one place that maps a *status* to how it looks.

Templates never choose colours.  A status string (``"published"``) maps to a **tone**
(``success | warning | danger | info | neutral``, the theme engine's semantic tokens), an icon and a
default label.  Because status is also an icon + text, it never relies on colour alone.
"""

from __future__ import annotations

from dataclasses import dataclass

TONES = ("success", "warning", "danger", "info", "neutral")


@dataclass(frozen=True)
class StatusInfo:
    tone: str
    icon: str
    label: str


STATUSES: dict[str, StatusInfo] = {
    "active": StatusInfo("success", "circle-check", "Active"),
    "inactive": StatusInfo("neutral", "circle-off", "Inactive"),
    "pending": StatusInfo("warning", "clock", "Pending"),
    "draft": StatusInfo("warning", "pencil", "Draft"),
    "published": StatusInfo("info", "send", "Published"),
    "superseded": StatusInfo("neutral", "history", "Superseded"),
    "assigned": StatusInfo("info", "user-check", "Assigned"),
    "cancelled": StatusInfo("neutral", "circle-x", "Cancelled"),
    "submitted": StatusInfo("success", "circle-check", "Submitted"),
    "overdue": StatusInfo("danger", "alarm-clock", "Overdue"),
    "archived": StatusInfo("neutral", "archive", "Archived"),
    "completed": StatusInfo("success", "circle-check-big", "Completed"),
    "in_progress": StatusInfo("info", "loader", "In progress"),
    "overdue": StatusInfo("danger", "clock-alert", "Overdue"),
    "open": StatusInfo("info", "circle-dot", "Open"),
    "closed": StatusInfo("neutral", "circle-slash", "Closed"),
    "acknowledged": StatusInfo("info", "eye", "Acknowledged"),
    "blocked": StatusInfo("warning", "octagon-alert", "Blocked"),
    "reopened": StatusInfo("warning", "rotate-ccw", "Reopened"),
    "resolved": StatusInfo("success", "circle-check", "Resolved"),
    "verified": StatusInfo("success", "badge-check", "Verified"),
    "rejected": StatusInfo("danger", "circle-x", "Rejected"),
    "suspended": StatusInfo("warning", "circle-pause", "Suspended"),
    "left": StatusInfo("neutral", "log-out", "Left"),
    "success": StatusInfo("success", "circle-check", "Success"),
    "warning": StatusInfo("warning", "triangle-alert", "Warning"),
    "danger": StatusInfo("danger", "circle-x", "Danger"),
    "info": StatusInfo("info", "info", "Info"),
    "neutral": StatusInfo("neutral", "circle", "Neutral"),
}

_FALLBACK = STATUSES["neutral"]


def status_info(value: object) -> StatusInfo:
    """Look a status up (case/space-insensitive); unknown values fall back to *neutral*."""
    key = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return STATUSES.get(key, _FALLBACK)


ICON_SIZES = {"xs": "h-3.5 w-3.5", "sm": "h-4 w-4", "md": "h-5 w-5", "lg": "h-6 w-6", "xl": "h-8 w-8"}
