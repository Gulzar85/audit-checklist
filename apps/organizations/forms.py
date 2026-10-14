"""Forms for the organization domain."""

from __future__ import annotations

from django import forms

from .models import Organization


class OrganizationForm(forms.ModelForm):
    """Create/update form for an organization.

    Server-side validation and error rendering are handled by Django + crispy;
    business validation (code normalisation) runs through ``Organization.clean``
    via ``ModelForm._post_clean``.
    """

    class Meta:
        model = Organization
        fields = (
            "name",
            "code",
            "slug",
            "legal_name",
            "description",
            "status",
            "logo",
            "favicon",
            "website",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "country",
            "timezone",
            "currency",
            "language",
            "date_format",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "address_line1": forms.TextInput(attrs={"autocomplete": "address-line1"}),
            "address_line2": forms.TextInput(attrs={"autocomplete": "address-line2"}),
            "city": forms.TextInput(attrs={"autocomplete": "address-level2"}),
            "state": forms.TextInput(attrs={"autocomplete": "address-level1"}),
            "postal_code": forms.TextInput(attrs={"autocomplete": "postal-code"}),
            "country": forms.TextInput(attrs={"autocomplete": "country"}),
            "phone": forms.TextInput(attrs={"autocomplete": "tel"}),
        }

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.fields["code"].help_text = "Leave empty to derive from the slug."
        self.fields["code"].required = False


class OrganizationStatusForm(forms.Form):
    """Validates an organization lifecycle transition request."""

    status = forms.ChoiceField(choices=Organization.Status.choices)
