"""Forms for the organization domain."""

from __future__ import annotations

from zoneinfo import available_timezones

from django import forms
from django.conf import settings

from apps.core.forms import DatalistInput, DesignSystemFormMixin, SectionedFormMixin

from .models import Organization


def _timezone_names() -> list[str]:
    return sorted(available_timezones())


def _language_codes() -> list[str]:
    return sorted({code for code, _name in settings.LANGUAGES})


class OrganizationForm(SectionedFormMixin, DesignSystemFormMixin, forms.ModelForm):
    """Create / edit an organization's details (branding has its own form).

    Server-side validation lives on the model (``Organization.clean_fields``) and
    is re-run by the service layer; this form only shapes the input.  ``code`` is
    the stable business key, so it is shown read-only once the organization exists.
    """

    sections = (
        ("Identity", ("name", "code", "slug", "legal_name", "description")),
        ("Contact", ("website", "email", "phone")),
        (
            "Address",
            ("address_line1", "address_line2", "city", "state", "postal_code", "country"),
        ),
        ("Regional settings", ("timezone", "currency", "language", "date_format")),
    )

    class Meta:
        model = Organization
        fields = (
            "name",
            "code",
            "slug",
            "legal_name",
            "description",
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
        labels = {
            "address_line1": "Address line 1",
            "address_line2": "Address line 2",
            "state": "State / province",
            "country": "Country code",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "address_line1": forms.TextInput(attrs={"autocomplete": "address-line1"}),
            "address_line2": forms.TextInput(attrs={"autocomplete": "address-line2"}),
            "city": forms.TextInput(attrs={"autocomplete": "address-level2"}),
            "state": forms.TextInput(attrs={"autocomplete": "address-level1"}),
            "postal_code": forms.TextInput(attrs={"autocomplete": "postal-code"}),
            "country": forms.TextInput(attrs={"autocomplete": "country", "maxlength": 2, "placeholder": "GB"}),
            "phone": forms.TextInput(attrs={"autocomplete": "tel"}),
            "currency": forms.TextInput(attrs={"maxlength": 3, "placeholder": "USD"}),
            "timezone": DatalistInput(_timezone_names, attrs={"placeholder": "Europe/London"}),
            "language": DatalistInput(_language_codes, attrs={"placeholder": "en"}),
        }

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.fields["code"].required = False
        self.fields["slug"].required = False
        if self.instance.pk and not self.instance._state.adding:
            self.fields["code"].disabled = True
            self.fields["code"].help_text = "The code is permanent and cannot be changed."
        else:
            self.fields["code"].help_text = "Stable unique code. Leave empty to derive it from the name."
        self.fields["slug"].help_text = "Readable unique handle. Leave empty to derive it from the name."
        self.fields["date_format"].required = False


class OrganizationBrandingForm(DesignSystemFormMixin, forms.ModelForm):
    """Logo, favicon and brand colour.

    Upload validation (extension allow-list, size cap, real-image check) lives in
    the model field validators, so the same rules apply to every entry point.
    """

    class Meta:
        model = Organization
        fields = ("logo", "favicon", "primary_color")
        widgets = {
            "logo": forms.ClearableFileInput(attrs={"accept": "image/png,image/jpeg,image/webp,image/gif"}),
            "favicon": forms.ClearableFileInput(attrs={"accept": "image/png,image/jpeg,image/webp,image/gif"}),
            "primary_color": forms.TextInput(attrs={"placeholder": "#4F46E5", "maxlength": 7}),
        }
        help_texts = {
            "logo": "PNG, JPEG, WebP or GIF, up to 2 MB.",
            "favicon": "Square image, ideally 64×64 or larger. Up to 2 MB.",
        }


class OrganizationStatusForm(forms.Form):
    """Validates an organization lifecycle transition request."""

    status = forms.ChoiceField(choices=Organization.Status.choices)
