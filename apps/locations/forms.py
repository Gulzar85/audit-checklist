"""Forms for the location domain.

The parent and location-type choices are scoped to the selected organization so
the UI cannot offer cross-organization options; the domain model still validates
on save as defence in depth.
"""

from __future__ import annotations

from django import forms

from apps.organizations.models import Organization
from apps.organizations.selectors import organizations_visible_to

from .models import Location, LocationType
from .selectors import available_location_types


class LocationTypeForm(forms.ModelForm):
    """Create/update a location type."""

    class Meta:
        model = LocationType
        fields = ("organization", "name", "code", "description", "sort_order", "is_active")
        widgets = {"description": forms.TextInput(attrs={"placeholder": "Optional"})}

    def __init__(self, *args, user=None, **kwargs) -> None:  # noqa: ANN001, ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.fields["organization"].required = False
        self.fields["organization"].help_text = (
            "Leave empty for a platform-defined type available to every organization."
        )
        if user is not None:
            self.fields["organization"].queryset = organizations_visible_to(user)


class LocationForm(forms.ModelForm):
    """Create/update a location."""

    class Meta:
        model = Location
        fields = (
            "organization",
            "parent",
            "location_type",
            "name",
            "code",
            "description",
            "is_active",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "country",
            "latitude",
            "longitude",
            "timezone",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "address_line1": forms.TextInput(attrs={"autocomplete": "address-line1"}),
            "address_line2": forms.TextInput(attrs={"autocomplete": "address-line2"}),
            "city": forms.TextInput(attrs={"autocomplete": "address-level2"}),
            "state": forms.TextInput(attrs={"autocomplete": "address-level1"}),
            "postal_code": forms.TextInput(attrs={"autocomplete": "postal-code"}),
            "country": forms.TextInput(attrs={"autocomplete": "country"}),
        }

    def __init__(self, *args, user=None, organization=None, **kwargs) -> None:  # noqa: ANN001, ANN002, ANN003
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields["organization"].queryset = organizations_visible_to(user)

        # A location never changes organization once created (children and types
        # would become inconsistent); keep the field fixed on edit.
        if self.instance.pk:
            self.fields["organization"].disabled = True

        resolved = organization
        if resolved is None and self.instance.pk:
            resolved = self.instance.organization
        if resolved is None:
            raw = self.data.get("organization") if self.is_bound else self.initial.get("organization")
            if raw:
                resolved = raw if isinstance(raw, Organization) else Organization.objects.filter(pk=raw).first()

        if resolved is not None:
            parent_qs = Location.objects.filter(organization=resolved).order_by("name")
            if self.instance.pk:
                parent_qs = parent_qs.exclude(pk=self.instance.pk)
            self.fields["parent"].queryset = parent_qs
            self.fields["location_type"].queryset = available_location_types(resolved)
        else:
            self.fields["parent"].queryset = Location.objects.none()
            self.fields["location_type"].queryset = LocationType.objects.none()


class LocationStatusForm(forms.Form):
    """Validates an activate/deactivate request."""

    action = forms.ChoiceField(choices=(("activate", "Activate"), ("deactivate", "Deactivate")))
