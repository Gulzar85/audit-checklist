"""Account-related forms."""

from __future__ import annotations

from django.contrib.auth.forms import AuthenticationForm, UsernameField
from django import forms


class EmailAuthenticationForm(AuthenticationForm):
    """Authentication form that labels the identifier as an email address."""

    username = UsernameField(
        label="Email address",
        widget=forms.EmailInput(
            attrs={"autofocus": True, "autocomplete": "email", "class": "input"}
        ),
    )
