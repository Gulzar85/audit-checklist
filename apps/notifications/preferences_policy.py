"""Which user preferences may suppress a notification (ADR 0081, docs/notifications-preferences-and-policy.md).

| Rule policy   | In-app copy                | Email                       |
|---------------|----------------------------|-----------------------------|
| ``optional``    | the user's in-app switch   | the user's email switch     |
| ``operational`` | **always shown**           | the user's email switch     |
| ``mandatory``   | **always shown**           | **always sent**             |

``mandatory`` is defined by the organization's own approved policy (it needs the separate ``notification.mandatory`` permission to set); the platform invents no
legal obligation.  A suppressed channel is *recorded* (status ``suppressed`` with the reason), never silently dropped.  Preferences are per user, organization and
category; no row means everything is on.
"""

from __future__ import annotations

from typing import Any

from . import constants as k
from .models import UserNotificationPreference


def decide(user: Any, organization: Any, category: str, policy: str, channels: set[str] | list[str]) -> dict[str, str | None]:
    """``{channel: suppression reason or None}`` for both channels (callers only act on the channels the rule requested)."""
    pref = UserNotificationPreference.objects.filter(user=user, organization=organization, category=category).first()
    in_app_on = True if pref is None else pref.in_app_enabled
    email_on = True if pref is None else pref.email_enabled
    out: dict[str, str | None] = {k.Channel.IN_APP: None, k.Channel.EMAIL: None}
    if policy == k.PreferencePolicy.OPTIONAL and not in_app_on:
        out[k.Channel.IN_APP] = "preference_in_app_off"
    if policy in (k.PreferencePolicy.OPTIONAL, k.PreferencePolicy.OPERATIONAL) and not email_on:
        out[k.Channel.EMAIL] = "preference_email_off"
    return out


def matrix(user: Any, organization: Any) -> list[dict[str, Any]]:
    """The user's effective choices per category (for the preferences screen and API)."""
    prefs = {p.category: p for p in UserNotificationPreference.objects.filter(user=user, organization=organization)}
    return [{"category": value, "label": str(label), "in_app_enabled": prefs[value].in_app_enabled if value in prefs else True,
             "email_enabled": prefs[value].email_enabled if value in prefs else True} for value, label in k.Category.choices]
