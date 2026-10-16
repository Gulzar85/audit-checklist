"""Forms for the checklist builder.  Forms shape and validate input only: authorization is a policy
decision made by the view, and every write is a service call (which enforces the data rules again)."""

from __future__ import annotations

from typing import Any

from django import forms
from django.core.exceptions import ValidationError

from apps.core.forms import DesignSystemFormMixin
from apps.locations.models import normalize_code, validate_code
from apps.organizations.models import Organization

from . import constants as c
from . import selectors
from .models import ChecklistCategory


class TemplateForm(DesignSystemFormMixin, forms.Form):
    organization = forms.ModelChoiceField(queryset=Organization.objects.none())
    name = forms.CharField(max_length=200)
    code = forms.CharField(max_length=40, help_text="Stable identifier, e.g. FS-001. It cannot be changed later.")
    category = forms.ModelChoiceField(queryset=ChecklistCategory.objects.none(), required=False)
    new_category = forms.CharField(max_length=80, required=False, label="Or add a new category")
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    instructions = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 4}),
        help_text="General guidance shown before the checklist starts.",
    )
    estimated_duration_minutes = forms.IntegerField(required=False, min_value=1, max_value=1440, label="Estimated duration (minutes)")

    def __init__(self, *args: Any, user: Any, template: Any = None, organization: Any = None, **kwargs: Any) -> None:
        self.template = template
        if template is not None and not kwargs.get("initial"):
            kwargs["initial"] = {
                "organization": template.organization_id, "name": template.name, "code": template.code,
                "category": template.category_id, "description": template.description,
                "instructions": template.instructions, "estimated_duration_minutes": template.estimated_duration_minutes,
            }
        super().__init__(*args, **kwargs)
        from apps.permissions.registry import P

        if template is not None:  # editing: organization and code are fixed
            self.fields["organization"].queryset = Organization.objects.filter(pk=template.organization_id)
            self.fields["organization"].disabled = True
            self.fields["code"].disabled = True
            orgs = [template.organization_id]
        else:
            orgs = selectors.organization_ids_with(user, P.CHECKLIST_CREATE)
            self.fields["organization"].queryset = Organization.objects.filter(pk__in=orgs, status="active").order_by("name")
            if organization is not None and str(organization.pk) in {str(o) for o in orgs}:
                self.fields["organization"].initial = organization.pk
                self.fields["code"].initial = selectors.next_code_hint(organization)
        self.fields["category"].queryset = ChecklistCategory.objects.filter(organization_id__in=orgs)

    def clean_code(self) -> str:
        if self.template is not None:
            return self.template.code
        code = normalize_code(self.cleaned_data["code"])
        try:
            validate_code(code)
        except ValidationError as exc:
            raise forms.ValidationError(exc.messages) from exc
        return code

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        organization, category = cleaned.get("organization"), cleaned.get("category")
        if category is not None and organization is not None and category.organization_id != organization.pk:
            self.add_error("category", "This category belongs to another organization.")
        return cleaned


class DuplicateForm(DesignSystemFormMixin, forms.Form):
    name = forms.CharField(max_length=200)
    code = forms.CharField(max_length=40, help_text="Must be unique in the organization.")

    def clean_code(self) -> str:
        code = normalize_code(self.cleaned_data["code"])
        try:
            validate_code(code)
        except ValidationError as exc:
            raise forms.ValidationError(exc.messages) from exc
        return code


class SectionForm(DesignSystemFormMixin, forms.Form):
    name = forms.CharField(max_length=200)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    instructions = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    is_required = forms.BooleanField(required=False, initial=True, label="Required section")

    def __init__(self, *args: Any, section: Any = None, **kwargs: Any) -> None:
        if section is not None and not kwargs.get("initial"):
            kwargs["initial"] = {"name": section.name, "description": section.description,
                                 "instructions": section.instructions, "is_required": section.is_required}
        super().__init__(*args, **kwargs)


class ItemForm(DesignSystemFormMixin, forms.Form):
    """Item editor.  Type-specific fields are shown/hidden client-side but *always validated here*."""

    question = forms.CharField(max_length=500, label="Question")
    code = forms.CharField(max_length=40, required=False, help_text="Stable identifier (e.g. TEMP-001). Generated if left blank.")
    answer_type = forms.ChoiceField(choices=c.AnswerType.choices, initial=c.AnswerType.YES_NO)
    is_required = forms.BooleanField(required=False, initial=True, label="Required")
    is_active = forms.BooleanField(required=False, initial=True, label="Active")
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    help_text = forms.CharField(required=False, max_length=500, label="Help text")
    instructions = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    # type-specific configuration (placeholders for Phase 5 validation)
    score_min = forms.IntegerField(required=False, label="Minimum score", min_value=0, max_value=100)
    score_max = forms.IntegerField(required=False, label="Maximum score", min_value=0, max_value=100)
    numeric_min = forms.FloatField(required=False, label="Minimum value")
    numeric_max = forms.FloatField(required=False, label="Maximum value")
    numeric_unit = forms.CharField(required=False, max_length=20, label="Unit")
    numeric_decimals = forms.IntegerField(required=False, min_value=0, max_value=6, label="Decimal places")
    max_length = forms.IntegerField(required=False, min_value=1, max_value=10000, label="Maximum length")
    matrix_rows = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Matrix rows (one per line)")
    matrix_columns = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Matrix columns (one per line)")

    def __init__(self, *args: Any, item: Any = None, **kwargs: Any) -> None:
        if item is not None and not kwargs.get("initial"):
            cfg = item.config or {}
            kwargs["initial"] = {
                "question": item.question, "code": item.code, "answer_type": item.answer_type,
                "is_required": item.is_required, "is_active": item.is_active, "description": item.description,
                "help_text": item.help_text, "instructions": item.instructions,
                "score_min": cfg.get("min") if item.answer_type == c.AnswerType.SCORE else None,
                "score_max": cfg.get("max") if item.answer_type == c.AnswerType.SCORE else None,
                "numeric_min": cfg.get("min") if item.answer_type == c.AnswerType.NUMERIC else None,
                "numeric_max": cfg.get("max") if item.answer_type == c.AnswerType.NUMERIC else None,
                "numeric_unit": cfg.get("unit"), "numeric_decimals": cfg.get("decimals"), "max_length": cfg.get("max_length"),
                "matrix_rows": "\n".join(cfg.get("rows", [])), "matrix_columns": "\n".join(cfg.get("columns", [])),
            }
        super().__init__(*args, **kwargs)
        self.item = item
        if item is not None:
            self.fields["code"].help_text = "Changing a code affects reports and integrations that use it."

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        answer_type = cleaned.get("answer_type")
        if not answer_type:
            return cleaned
        data = cleaned
        if answer_type == c.AnswerType.SCORE:
            raw: dict[str, Any] = {k: v for k, v in (("min", data.get("score_min")), ("max", data.get("score_max"))) if v is not None}
        elif answer_type == c.AnswerType.NUMERIC:
            raw = {k: v for k, v in (("min", data.get("numeric_min")), ("max", data.get("numeric_max")),
                                     ("unit", data.get("numeric_unit")), ("decimals", data.get("numeric_decimals"))) if v not in (None, "")}
        elif answer_type in (c.AnswerType.TEXT, c.AnswerType.LONG_TEXT):
            raw = {"max_length": data["max_length"]} if data.get("max_length") else {}
        elif answer_type == c.AnswerType.MATRIX:
            raw = {"rows": [x.strip() for x in (data.get("matrix_rows") or "").splitlines() if x.strip()],
                   "columns": [x.strip() for x in (data.get("matrix_columns") or "").splitlines() if x.strip()]}
        else:
            raw = {}
        try:
            cleaned["config"] = c.clean_config(answer_type, raw)
        except ValidationError as exc:
            self.add_error(None, exc.messages)
        return cleaned


class OptionForm(DesignSystemFormMixin, forms.Form):
    label = forms.CharField(max_length=200)


class VersionForm(DesignSystemFormMixin, forms.Form):
    change_summary = forms.CharField(max_length=300, required=False, label="What changes in this version?")
