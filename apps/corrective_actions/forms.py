"""Forms for the corrective-action screens.  They only shape and pre-validate input; every rule is enforced by the services."""

from __future__ import annotations

from typing import Any

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model

from apps.core.forms import DesignSystemFormMixin

from . import selectors
from .constants import Priority, Purpose
from .models import CorrectiveActionUpdate

DATETIME_FORMATS = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"]


def due_field() -> forms.DateTimeField:
    return forms.DateTimeField(
        required=False, label="Due", input_formats=DATETIME_FORMATS,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        help_text="Optional. In the application's time zone. Must be in the future.",
    )


def _is_uuid(value: Any) -> bool:
    import uuid

    try:
        uuid.UUID(str(getattr(value, "pk", value)))
    except ValueError:
        return False
    return True


class ActionForm(DesignSystemFormMixin, forms.Form):
    """A new action against a finding."""

    finding = forms.ModelChoiceField(queryset=None, label="Finding", empty_label="Choose a finding…")
    title = forms.CharField(max_length=200, label="Title")
    description = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3}), label="Description")
    plan = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3}), label="Planned activities")
    expected_outcome = forms.CharField(required=False, max_length=500, label="Expected outcome")
    completion_criteria = forms.CharField(required=False, max_length=500, label="Completion criteria")
    priority = forms.ChoiceField(choices=Priority.choices, initial=Priority.NORMAL)
    assigned_to = forms.ModelChoiceField(queryset=get_user_model().objects.none(), required=False, label="Responsible user", empty_label="No one yet")
    due_at = due_field()
    required_evidence = forms.IntegerField(min_value=0, max_value=10, required=False, label="Completion files required",
                                           help_text="Verified files the owner must attach before submitting for verification.")

    def __init__(self, *args: Any, user: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["finding"].queryset = selectors.creatable_findings(user)
        self.fields["required_evidence"].initial = int(getattr(settings, "CORRECTIVE_ACTION_DEFAULT_REQUIRED_EVIDENCE", 0))
        raw = self.data.get("finding") if self.is_bound else self.initial.get("finding")
        finding = self.fields["finding"].queryset.filter(pk=getattr(raw, "pk", raw)).first() if raw and _is_uuid(raw) else None
        if finding is not None:
            self.fields["assigned_to"].queryset = selectors.eligible_assignees(finding.organization, finding.location)


class EditForm(DesignSystemFormMixin, forms.Form):
    title = forms.CharField(max_length=200, label="Title")
    description = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3}), label="Description")
    plan = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3}), label="Planned activities")
    expected_outcome = forms.CharField(required=False, max_length=500, label="Expected outcome")
    completion_criteria = forms.CharField(required=False, max_length=500, label="Completion criteria")
    priority = forms.ChoiceField(choices=Priority.choices)
    due_at = due_field()
    required_evidence = forms.IntegerField(min_value=0, max_value=10, label="Completion files required")

    def __init__(self, *args: Any, manager: bool, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not manager:  # the responsible user edits the plan only
            for name in ("title", "description", "priority", "due_at", "required_evidence"):
                del self.fields[name]


class ReasonForm(DesignSystemFormMixin, forms.Form):
    reason = forms.CharField(required=False, max_length=1000, label="Reason", widget=forms.Textarea(attrs={"rows": 3}))


class AssignForm(forms.Form):
    assigned_to = forms.UUIDField(required=False)


class UpdateForm(DesignSystemFormMixin, forms.Form):
    body = forms.CharField(max_length=2000, label="What was done", widget=forms.Textarea(attrs={"rows": 3}))
    progress_percent = forms.IntegerField(required=False, min_value=0, max_value=100, label="Progress (%)",
                                          help_text="Optional. Your own estimate - it never changes the status and is not verification.")
    corrects = forms.ModelChoiceField(queryset=CorrectiveActionUpdate.objects.none(), required=False, label="Correct an earlier entry",
                                      empty_label="This is a new entry")

    def __init__(self, *args: Any, action: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["corrects"].queryset = CorrectiveActionUpdate.objects.filter(action=action)


class EvidenceForm(forms.Form):
    purpose = forms.ChoiceField(choices=Purpose.choices)
    caption = forms.CharField(required=False, max_length=200)
