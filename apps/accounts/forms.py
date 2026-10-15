"""Account-related forms."""

from __future__ import annotations

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UsernameField

from apps.core.forms import DesignSystemFormMixin

from .models import User


class EmailAuthenticationForm(DesignSystemFormMixin, AuthenticationForm):
    """Authentication form that labels the identifier as an email address."""

    username = UsernameField(
        label="Email address",
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}),
    )


class UserProfileUpdateForm(DesignSystemFormMixin, forms.ModelForm):
    """Self-service edit of *application-managed* profile fields only.

    Identity-provider-owned fields (display name, names, job title, email) are
    synchronised from Entra ID and are deliberately not editable here.
    """

    class Meta:
        model = User
        fields = ("phone_number", "avatar")
        widgets = {
            "phone_number": forms.TextInput(attrs={"autocomplete": "tel"}),
        }


class PreferencesForm(DesignSystemFormMixin, forms.Form):
    """Self-service preferences (the allow-listed ones; appearance has its own page)."""

    timezone = forms.CharField(
        required=False, max_length=64, label="Time zone",
        help_text="IANA name such as Asia/Karachi. Leave empty to use your organization's.",
    )
    language = forms.CharField(
        required=False, max_length=10, label="Language",
        help_text="Language code such as en. Leave empty to use your organization's.",
    )

    def _clean_preference(self, key: str) -> str:
        from django.core.exceptions import ValidationError

        from .preferences import validate_preference

        value = (self.cleaned_data.get(key) or "").strip()
        if not value:
            return ""
        try:
            return validate_preference(key, value)
        except ValidationError as exc:
            raise forms.ValidationError(exc.message_dict.get(key) or exc.messages) from exc

    def clean_timezone(self) -> str:
        return self._clean_preference("timezone")

    def clean_language(self) -> str:
        return self._clean_preference("language")
