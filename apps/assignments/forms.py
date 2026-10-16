"""Forms for the assignment screens.  They only shape and pre-validate input; every rule is enforced by the services."""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib.auth import get_user_model

from apps.checklists.models import ChecklistVersion
from apps.core.forms import DesignSystemFormMixin

from . import selectors
from .constants import AssignmentPriority

DATETIME_FORMATS = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"]


def due_field() -> forms.DateTimeField:
    return forms.DateTimeField(
        required=False, label="Due", input_formats=DATETIME_FORMATS,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        help_text="Optional. In the application's time zone. Must be in the future.",
    )


class _VersionChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj: Any) -> str:
        return f"{obj.template.name} · version {obj.version_number} (current)"


class AssignmentForm(DesignSystemFormMixin, forms.Form):
    """One assignment: which checklist (its current published version), where, who, when."""

    version = _VersionChoice(queryset=ChecklistVersion.objects.none(), label="Checklist", empty_label="Choose a checklist…")
    location = forms.ModelChoiceField(queryset=None, label="Location", empty_label="Choose a location…")
    assigned_to = forms.ModelChoiceField(queryset=get_user_model().objects.none(), required=False, label="Responsible user",
                                         empty_label="No one in particular (the location's eligible users)")
    due_at = due_field()
    priority = forms.ChoiceField(choices=AssignmentPriority.choices, initial=AssignmentPriority.NORMAL)
    instructions = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3}), label="Instructions")

    def __init__(self, *args: Any, user: Any, assignment: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.user = user
        versions = selectors.assignable_versions(user)
        if assignment is not None and assignment.version not in versions:
            versions = [assignment.version, *versions]  # a draft keeps showing its (possibly superseded) version; issuing revalidates
        self.fields["version"].queryset = ChecklistVersion.objects.filter(pk__in=[v.pk for v in versions]).select_related("template")
        self.fields["location"].queryset = selectors.assignable_locations(user)
        location = self._location()
        if location is not None:
            self.fields["assigned_to"].queryset = selectors.eligible_assignees(location.organization, location)

    def _location(self) -> Any:
        raw = self.data.get("location") if self.is_bound else (self.initial.get("location") or None)
        if not raw:
            return None
        return self.fields["location"].queryset.filter(pk=getattr(raw, "pk", raw)).first() if _is_uuid(raw) else None


def _is_uuid(value: Any) -> bool:
    import uuid

    try:
        uuid.UUID(str(getattr(value, "pk", value)))
    except ValueError:
        return False
    return True


class BulkForm(DesignSystemFormMixin, forms.Form):
    version = _VersionChoice(queryset=ChecklistVersion.objects.none(), label="Checklist", empty_label="Choose a checklist…")
    include_descendants = forms.BooleanField(required=False, label="Include the locations beneath each selected one",
                                             help_text="Only locations you are allowed to assign to are added; the rest are listed as left out.")
    due_at = due_field()
    priority = forms.ChoiceField(choices=AssignmentPriority.choices, initial=AssignmentPriority.NORMAL)
    instructions = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3}), label="Instructions")
    issue = forms.BooleanField(required=False, initial=True, label="Issue immediately", help_text="Untick to create drafts you can review and issue later.")

    def __init__(self, *args: Any, user: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        ids = [v.pk for v in selectors.assignable_versions(user)]
        self.fields["version"].queryset = ChecklistVersion.objects.filter(pk__in=ids).select_related("template")


class CancelForm(DesignSystemFormMixin, forms.Form):
    reason = forms.CharField(max_length=300, label="Reason", widget=forms.Textarea(attrs={"rows": 2}))


class PriorityForm(forms.Form):
    priority = forms.ChoiceField(choices=AssignmentPriority.choices)


class DueForm(forms.Form):
    due_at = forms.DateTimeField(required=False, input_formats=DATETIME_FORMATS)


class AssigneeForm(forms.Form):
    assigned_to = forms.UUIDField(required=False)
