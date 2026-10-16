"""Forms for the checklist builder.  Forms shape and validate input only: authorization is a policy
decision made by the view, and every write is a service call (which enforces the data rules again)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from django import forms
from django.utils import timezone
from django.core.exceptions import ValidationError

from apps.core.forms import DesignSystemFormMixin
from apps.locations.models import normalize_code, validate_code
from apps.organizations.models import Organization

from . import constants as c
from . import selectors
from .answer_types import registry
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
    """The *general* part of the item editor.  Type-specific settings are a separate, dynamically built form
    (:func:`build_config_form`) so this form never grows a field per answer type."""

    question = forms.CharField(max_length=500, label="Question")
    code = forms.CharField(max_length=40, required=False, help_text="Stable identifier (e.g. TEMP-001). Generated if left blank.")
    answer_type = forms.ChoiceField(choices=registry.choices, initial=c.AnswerType.YES_NO)
    is_required = forms.BooleanField(required=False, initial=True, label="Required")
    is_active = forms.BooleanField(required=False, initial=True, label="Active")
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    help_text = forms.CharField(required=False, max_length=500, label="Help text")
    instructions = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args: Any, item: Any = None, **kwargs: Any) -> None:
        if item is not None and not kwargs.get("initial"):
            kwargs["initial"] = {
                "question": item.question, "code": item.code, "answer_type": item.answer_type,
                "is_required": item.is_required, "is_active": item.is_active, "description": item.description,
                "help_text": item.help_text, "instructions": item.instructions,
            }
        super().__init__(*args, **kwargs)
        self.item = item
        if item is not None:
            self.fields["code"].help_text = "Changing a code affects reports and integrations that use it."
        # The selector shows what each type is for (the description is also read by assistive technology).
        self.fields["answer_type"].widget.attrs.update({"aria-describedby": "answer-type-help"})


# ---------------------------------------------------------------------------
# Answer-type configuration: a form generated from the type's schema
# ---------------------------------------------------------------------------
_CONFIG_PREFIX = "cfg"


class _ConfigFormBase(DesignSystemFormMixin, forms.Form):
    """Base of the generated configuration form; subclasses are built per answer type."""

    definition: Any = None

    def to_config(self) -> dict[str, Any]:
        """The raw configuration object for the service (the engine validates and normalizes it)."""
        raw: dict[str, Any] = {}
        for spec in self.definition.config_fields:
            value = self.cleaned_data.get(spec.key)
            if spec.kind == "labels":
                value = [line.strip() for line in (value or "").splitlines() if line.strip()]
            elif spec.kind in ("date", "datetime") and value:
                value = value.isoformat()
            if value is None or value == "" or (value == [] and spec.kind in ("list", "labels")):
                continue
            raw[spec.key] = value
        return raw


def _form_field(spec: Any) -> forms.Field:
    common = {"required": False, "label": spec.label, "help_text": spec.help}
    if spec.kind == "int":
        return forms.IntegerField(min_value=spec.min, max_value=spec.max, **common)
    if spec.kind == "number":
        return forms.DecimalField(max_digits=18, decimal_places=6, **common)
    if spec.kind == "text":
        return forms.CharField(max_length=spec.max_length, **common)
    if spec.kind == "bool":
        return forms.BooleanField(**common)
    if spec.kind == "choice":
        return forms.ChoiceField(choices=spec.choices, **common)
    if spec.kind == "list":
        return forms.MultipleChoiceField(
            choices=[(v, v.upper()) for v in spec.universe], widget=forms.CheckboxSelectMultiple, **common
        )
    if spec.kind == "labels":
        return forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), max_length=4000, **common)
    if spec.kind == "date":
        return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"), **common)
    if spec.kind == "datetime":
        return forms.DateTimeField(
            widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"], **common,
        )
    raise ValueError(f"No form field for setting kind {spec.kind!r}.")  # a schema bug, caught by tests


def _initial_for(spec: Any, value: Any) -> Any:
    if value is None:
        return None
    if spec.kind == "labels":
        return "\n".join(entry["label"] if isinstance(entry, dict) else str(entry) for entry in value)
    if spec.kind == "date":
        return date.fromisoformat(value)
    if spec.kind == "datetime":
        return datetime.fromisoformat(value).astimezone(timezone.get_current_timezone()).replace(tzinfo=None)
    return value


def build_config_form(answer_type: str, data: Any = None, config: dict[str, Any] | None = None) -> _ConfigFormBase:
    """Build the configuration form of ``answer_type`` (bound when ``data`` is given).

    ``config`` is the stored configuration used as initial values; for a type without settings the form has no
    fields.  Unknown types fall back to the default type so a tampered ``answer_type`` never raises here (the
    service rejects it with a proper error).
    """
    if answer_type not in registry:
        answer_type = c.AnswerType.YES_NO
    definition = registry.get(answer_type)
    initial = {**definition.defaults(), **(config or {})}
    fields = {spec.key: _form_field(spec) for spec in definition.config_fields}
    form_class = type(f"{answer_type.title().replace('_', '')}ConfigForm", (_ConfigFormBase,), {"definition": definition, **fields})
    form = form_class(data=data, prefix=_CONFIG_PREFIX,
                      initial={spec.key: _initial_for(spec, initial.get(spec.key)) for spec in definition.config_fields})
    return form


class OptionForm(DesignSystemFormMixin, forms.Form):
    label = forms.CharField(max_length=200)


class VersionForm(DesignSystemFormMixin, forms.Form):
    change_summary = forms.CharField(max_length=300, required=False, label="What changes in this version?")
