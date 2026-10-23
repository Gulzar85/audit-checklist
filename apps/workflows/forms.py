"""Forms for the workflow screens.  They shape and pre-validate input only; every rule is enforced by the services.

The designer never types an executable expression: conditions are built from a registered attribute, a Phase 6 operator and a value; handlers,
policies and separation options are choices from the registries.  An optional *advanced* JSON field accepts the same controlled condition tree and is validated by the same code.
"""

from __future__ import annotations

import json
from typing import Any

from django import forms

from apps.checklists.rules.operators import OPERATORS
from apps.core.forms import DesignSystemFormMixin
from apps.permissions import registry as permission_registry

from . import conditions, handlers, targets
from .constants import SEPARATION_OPTIONS, ActorRule, ApprovalMode, AssigneeType, Decision, OnNoAssignee, StateKind


def _target_choices() -> list[tuple[str, str]]:
    return [(t, targets.get_spec(t).label) for t in targets.registered_types()]


class DefinitionForm(DesignSystemFormMixin, forms.Form):
    code = forms.SlugField(max_length=40, label="Code", help_text="Lowercase letters, digits, - and _. Cannot be changed later.")
    name = forms.CharField(max_length=120, label="Name")
    description = forms.CharField(required=False, max_length=500, label="Description", widget=forms.Textarea(attrs={"rows": 2}))
    target_type = forms.ChoiceField(label="Applies to", choices=[])
    is_default = forms.BooleanField(required=False, label="Default workflow for these records", help_text="Used when a record of this kind enters a workflow without naming one.")
    auto_start_event = forms.ChoiceField(required=False, label="Start automatically when", choices=[("", "Never (started explicitly)")])

    def __init__(self, *args: Any, editing: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["target_type"].choices = _target_choices()
        events = [(code, label) for t in targets.registered_types() for code, label in targets.get_spec(t).events.items()]
        self.fields["auto_start_event"].choices = [("", "Never (started explicitly)"), *events]
        if editing:
            for name in ("code", "target_type"):
                self.fields[name].disabled = True


class _ConditionMixin(forms.Form):
    """A single-comparison builder (attribute / operator / value) plus an advanced JSON tree; both validated by ``conditions.clean_condition``."""

    cond_attribute = forms.CharField(required=False, max_length=40, label="When this attribute")
    cond_operator = forms.ChoiceField(required=False, label="…", choices=[("", "")])
    cond_value = forms.CharField(required=False, max_length=200, label="…this value", help_text="Several values: comma-separated. A range: two values.")
    cond_json = forms.CharField(required=False, label="Advanced: condition as JSON", widget=forms.Textarea(attrs={"rows": 3}),
                                help_text="Optional. Overrides the fields above. AND/OR groups of the same comparisons.")

    def _init_condition(self, target_type: str) -> None:
        self.fields["cond_operator"].choices = [("", "—"), *[(c, op.label) for c, op in OPERATORS.items()]]
        attributes = targets.get_spec(target_type).attributes.values()
        self.fields["cond_attribute"] = forms.ChoiceField(required=False, label="When this attribute", choices=[("", "—"), *[(a.code, a.label) for a in attributes]])

    def build_condition(self, target_type: str) -> tuple[Any, str]:
        """``(condition | None, error)`` from the posted builder / JSON."""
        raw_json = (self.cleaned_data.get("cond_json") or "").strip()
        if raw_json:
            try:
                return json.loads(raw_json), ""
            except ValueError:
                return None, "The condition is not valid JSON."
        attribute, operator = (self.cleaned_data.get("cond_attribute") or "").strip(), self.cleaned_data.get("cond_operator") or ""
        if not attribute and not operator:
            return None, ""
        spec = targets.get_spec(target_type)
        attr, op = spec.attributes.get(attribute), OPERATORS.get(operator)
        if attr is None or op is None:
            return None, "Choose an attribute and an operator."
        text = (self.cleaned_data.get("cond_value") or "").strip()
        if op.operand == "none":
            value: Any = None
        else:
            parts = [p.strip() for p in text.split(",") if p.strip()]

            def one(p: str) -> Any:
                if attr.kind == "int":
                    return int(p) if p.lstrip("-").isdigit() else p
                if attr.kind == "bool":
                    return {"true": True, "yes": True, "false": False, "no": False}.get(p.lower(), p)
                return p

            value = [one(p) for p in parts] if op.operand in ("range", "options") else (one(parts[0]) if parts else "")
        return {"attribute": attribute, "operator": operator, "value": value}, ""


class VersionForm(_ConditionMixin, DesignSystemFormMixin, forms.Form):
    change_summary = forms.CharField(required=False, max_length=300, label="What changes in this version")
    allow_loops = forms.BooleanField(required=False, label="Allow loops (e.g. return for correction)")

    def __init__(self, *args: Any, target_type: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._init_condition(target_type)
        self.fields["cond_attribute"].label = "A record may start this workflow when this attribute"


class StateForm(DesignSystemFormMixin, forms.Form):
    code = forms.SlugField(max_length=40, label="Code")
    name = forms.CharField(max_length=120, label="Name")
    description = forms.CharField(required=False, max_length=300, label="Description")
    kind = forms.ChoiceField(choices=StateKind.choices, label="Kind")
    sort_order = forms.IntegerField(required=False, min_value=0, max_value=1000, label="Order")
    assignee_type = forms.ChoiceField(required=False, choices=[("", "No task (a waiting state)"), *AssigneeType.choices], label="Who gets a task")
    assignee_permission = forms.ChoiceField(required=False, choices=[], label="…holders of permission")
    assignee_role = forms.CharField(required=False, max_length=80, label="…holders of role (slug)")
    assignee_users = forms.CharField(required=False, label="…named users (ids, comma-separated)")
    approval_mode = forms.ChoiceField(choices=ApprovalMode.choices, initial=ApprovalMode.ONE, label="Approvals needed")
    min_approvals = forms.IntegerField(required=False, min_value=1, max_value=20, initial=1, label="Minimum approvals")
    separation = forms.MultipleChoiceField(required=False, choices=list(SEPARATION_OPTIONS.items()), widget=forms.CheckboxSelectMultiple, label="Separation of duties")
    due_hours = forms.IntegerField(required=False, min_value=1, max_value=24 * 365, label="Task deadline (hours after entering)")
    on_no_assignee = forms.ChoiceField(choices=OnNoAssignee.choices, label="If nobody is eligible")

    def __init__(self, *args: Any, editing: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["assignee_permission"].choices = [("", "—"), *[(p.code, p.label) for p in permission_registry.REGISTRY]]
        if editing:
            self.fields["code"].disabled = True

    def policy(self) -> Any:
        kind = self.cleaned_data.get("assignee_type")
        if kind == AssigneeType.PERMISSION:
            return {"type": "permission", "permission": self.cleaned_data.get("assignee_permission")}
        if kind == AssigneeType.ROLE:
            return {"type": "role", "role": self.cleaned_data.get("assignee_role")}
        if kind == AssigneeType.USER:
            return {"type": "user", "users": [u.strip() for u in (self.cleaned_data.get("assignee_users") or "").split(",") if u.strip()]}
        return None


class TransitionForm(_ConditionMixin, DesignSystemFormMixin, forms.Form):
    source = forms.ChoiceField(choices=[], label="From state")
    target = forms.ChoiceField(choices=[], label="To state")
    action = forms.SlugField(max_length=40, label="Action code")
    label = forms.CharField(max_length=80, label="Button label")
    decision = forms.ChoiceField(choices=Decision.choices, initial=Decision.OTHER, label="Counts as")
    actor_rule = forms.ChoiceField(choices=ActorRule.choices, label="Who may do it")
    handler = forms.ChoiceField(required=False, choices=[], label="Business action to perform")
    reason_required = forms.BooleanField(required=False, label="A reason is required")
    sort_order = forms.IntegerField(required=False, min_value=0, max_value=1000, label="Branch order")
    enabled = forms.BooleanField(required=False, initial=True, label="Enabled")

    def __init__(self, *args: Any, version: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._init_condition(version.definition.target_type)
        states = [(s.code, f"{s.name} ({s.code})") for s in version.states.order_by("sort_order", "code")]
        self.fields["source"].choices = self.fields["target"].choices = states
        self.fields["handler"].choices = [("", "None"), *[(h.code, h.label) for h in handlers.for_target(version.definition.target_type)]]
        self.target_type = version.definition.target_type


class ActionForm(DesignSystemFormMixin, forms.Form):
    action = forms.SlugField(max_length=40)
    comment = forms.CharField(required=False, max_length=1000, widget=forms.Textarea(attrs={"rows": 3}), label="Comment")
    idempotency_key = forms.UUIDField(required=False)
    expected_revision = forms.IntegerField(required=False, min_value=1)


class ReasonForm(DesignSystemFormMixin, forms.Form):
    reason = forms.CharField(max_length=300, label="Reason", widget=forms.Textarea(attrs={"rows": 2}))


class ReassignForm(forms.Form):
    assignee = forms.UUIDField()


_ = conditions
