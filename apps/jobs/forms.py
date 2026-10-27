"""The schedule form: validated field by field here, then handed to ``services.save_schedule`` (which validates everything again - the service is the authority)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from django import forms
from django.utils import timezone

from . import constants as k
from . import policies, recurrence, registry
from .errors import JobError

WEEKDAYS = [(0, "Mon"), (1, "Tue"), (2, "Wed"), (3, "Thu"), (4, "Fri"), (5, "Sat"), (6, "Sun")]
ZONES = ["UTC", "Asia/Karachi", "Asia/Dubai", "Asia/Kolkata", "Europe/London", "Europe/Paris", "America/New_York", "America/Chicago", "America/Los_Angeles", "Australia/Sydney"]


class ScheduleForm(forms.Form):
    name = forms.CharField(max_length=120)
    description = forms.CharField(max_length=500, required=False)
    task_type = forms.ChoiceField(choices=[])
    schedule_type = forms.ChoiceField(choices=k.ScheduleType.choices)
    timezone = forms.CharField(max_length=64, initial="UTC")
    start_at = forms.DateTimeField(input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"], help_text="In the schedule's time zone.")
    end_at = forms.DateTimeField(input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"], required=False)
    every_minutes = forms.IntegerField(required=False, min_value=k.MIN_INTERVAL_MINUTES, max_value=k.MAX_INTERVAL_MINUTES)
    time = forms.CharField(max_length=5, required=False, help_text="HH:MM, 24 hour")
    weekdays = forms.MultipleChoiceField(choices=WEEKDAYS, required=False, widget=forms.CheckboxSelectMultiple)
    day = forms.IntegerField(required=False, min_value=1, max_value=31)
    misfire_policy = forms.ChoiceField(choices=k.MisfirePolicy.choices, initial=k.MisfirePolicy.SKIP)
    max_attempts = forms.IntegerField(min_value=1, max_value=k.MAX_ATTEMPTS_LIMIT, initial=3)
    payload = forms.CharField(required=False, max_length=k.MAX_PAYLOAD_BYTES, widget=forms.Textarea(attrs={"rows": 3}), help_text="Optional task settings as JSON; unknown keys are refused.")
    platform_wide = forms.BooleanField(required=False)
    allow_timezone_change = forms.BooleanField(required=False)

    def __init__(self, *args: Any, user: Any = None, organization: Any = None, editing: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.user, self.organization, self.editing = user, organization, editing
        self.fields["task_type"].choices = [(t.code, t.name) for t in registry.REGISTRY.values() if t.schedulable and (policies.can_schedule(user, t, organization) or policies.can_schedule(user, t, None))]
        if editing:
            self.fields["task_type"].disabled = True
            self.fields["platform_wide"].disabled = True

    def clean_timezone(self) -> str:
        try:
            return recurrence.get_zone(self.cleaned_data["timezone"].strip()).key
        except JobError as exc:
            raise forms.ValidationError(exc.message) from exc

    def clean_payload(self) -> dict[str, Any]:
        raw = (self.cleaned_data.get("payload") or "").strip()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise forms.ValidationError("The settings must be valid JSON.") from exc
        if not isinstance(value, dict):
            raise forms.ValidationError("The settings must be a JSON object.")
        return value

    def clean(self) -> dict[str, Any]:
        data = super().clean()
        stype, zone_name = data.get("schedule_type"), data.get("timezone")
        if not stype or not zone_name:
            return data
        for field in ("start_at", "end_at"):
            value = data.get(field)
            if isinstance(value, datetime):  # the browser gives wall-clock time: it is read in the schedule's own zone
                data[field] = timezone.make_naive(value, timezone.get_default_timezone()).replace(tzinfo=recurrence.get_zone(zone_name)).astimezone(recurrence.UTC) if timezone.is_aware(value) else value.replace(tzinfo=recurrence.get_zone(zone_name)).astimezone(recurrence.UTC)
        rec: dict[str, Any] = {}
        if stype == k.ScheduleType.INTERVAL:
            rec = {"every_minutes": data.get("every_minutes")}
        elif stype in (k.ScheduleType.DAILY, k.ScheduleType.WEEKLY, k.ScheduleType.MONTHLY):
            rec = {"time": (data.get("time") or "").strip()}
            if stype == k.ScheduleType.WEEKLY:
                rec["weekdays"] = [int(d) for d in data.get("weekdays") or []]
            if stype == k.ScheduleType.MONTHLY:
                rec["day"] = data.get("day")
        data["recurrence"] = rec
        return data

    def service_data(self) -> dict[str, Any]:
        d = self.cleaned_data
        return {key: d.get(key) for key in ("name", "description", "task_type", "schedule_type", "timezone", "start_at", "end_at", "recurrence", "payload", "misfire_policy", "max_attempts")}
