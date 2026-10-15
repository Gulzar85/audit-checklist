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
