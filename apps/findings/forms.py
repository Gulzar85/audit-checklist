"""Forms for the findings screens.  They only shape and pre-validate input; every rule is enforced by the services."""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib.auth import get_user_model

from apps.core.forms import DesignSystemFormMixin

from . import selectors
from .constants import SourceType  # noqa: F401  (re-exported for templates/tests that build forms)
from .models import FindingCategory, FindingSeverity

DATETIME_FORMATS = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"]


def due_field(label: str = "Due") -> forms.DateTimeField:
    return forms.DateTimeField(
        required=False, label=label, input_formats=DATETIME_FORMATS,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        help_text="Optional. In the application's time zone. Must be in the future.",
    )


class FindingForm(DesignSystemFormMixin, forms.Form):
    """A manual finding: where, what, how serious and (optionally) who and when."""

    location = forms.ModelChoiceField(queryset=None, label="Location", empty_label="Choose a location…")
    title = forms.CharField(max_length=200, label="Title")
    description = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 4}), label="Description")
    category = forms.ModelChoiceField(queryset=FindingCategory.objects.none(), label="Category", empty_label="Choose a category…")
    severity = forms.ModelChoiceField(queryset=FindingSeverity.objects.none(), label="Severity", empty_label="Choose a severity…")
    assigned_to = forms.ModelChoiceField(queryset=get_user_model().objects.none(), required=False, label="Responsible user", empty_label="No one yet")
    due_at = due_field()

    def __init__(self, *args: Any, user: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = selectors.creatable_locations(user)
        self.fields["category"].queryset = selectors.visible_categories(user).filter(is_active=True).order_by("sort_order", "name")
        self.fields["severity"].queryset = selectors.visible_severities(user).filter(is_active=True).order_by("-rank")
        raw = self.data.get("location") if self.is_bound else self.initial.get("location")
        location = self.fields["location"].queryset.filter(pk=getattr(raw, "pk", raw)).first() if raw and _is_uuid(raw) else None
        if location is not None:
            self.fields["assigned_to"].queryset = selectors.eligible_assignees(location.organization, location)


class EditForm(DesignSystemFormMixin, forms.Form):
    title = forms.CharField(max_length=200, label="Title")
    description = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 4}), label="Description")
    category = forms.ModelChoiceField(queryset=FindingCategory.objects.none(), label="Category")
    severity = forms.ModelChoiceField(queryset=FindingSeverity.objects.none(), label="Severity")
    due_at = due_field()

    def __init__(self, *args: Any, finding: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = selectors.categories_for(finding.organization)
        self.fields["severity"].queryset = selectors.severities_for(finding.organization).order_by("-rank")


class ActionForm(DesignSystemFormMixin, forms.Form):
    reason = forms.CharField(required=False, max_length=1000, label="Reason", widget=forms.Textarea(attrs={"rows": 3}))


class AssignForm(forms.Form):
    assigned_to = forms.UUIDField(required=False)
    due_at = forms.DateTimeField(required=False, input_formats=DATETIME_FORMATS)


class CategoryForm(DesignSystemFormMixin, forms.Form):
    code = forms.CharField(max_length=40, label="Code", help_text="Lowercase letters, digits, - and _. Cannot be changed later.")
    name = forms.CharField(max_length=100, label="Name")
    description = forms.CharField(required=False, max_length=300, label="Description")
    sort_order = forms.IntegerField(required=False, min_value=0, label="Order")
    is_active = forms.BooleanField(required=False, initial=True, label="Active")


class SeverityForm(DesignSystemFormMixin, forms.Form):
    code = forms.CharField(max_length=40, label="Code", help_text="Lowercase letters, digits, - and _. Cannot be changed later.")
    name = forms.CharField(max_length=100, label="Name")
    rank = forms.IntegerField(min_value=1, max_value=100, label="Rank", help_text="1-100. A higher rank is more severe; this, not the name, orders levels.")
    requires_owner = forms.BooleanField(required=False, label="Needs a responsible user before work starts")
    default_due_days = forms.IntegerField(required=False, min_value=1, max_value=3650, label="Default due (days)", help_text="Leave empty for no automatic due date.")
    is_escalation = forms.BooleanField(required=False, label="Escalation level", help_text="Counted as critical on the dashboard; later phases may escalate it.")
    is_default = forms.BooleanField(required=False, label="Default level", help_text="Used when a rule names an unknown level.")
    is_active = forms.BooleanField(required=False, initial=True, label="Active")


def _is_uuid(value: Any) -> bool:
    import uuid

    try:
        uuid.UUID(str(getattr(value, "pk", value)))
    except ValueError:
        return False
    return True
