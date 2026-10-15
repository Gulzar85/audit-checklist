"""Forms for the theme engine.

The editor form is *generated from the token registry*, so there is exactly one list of
editable things (``apps/themes/tokens.py``) and no raw-CSS / raw-JSON field anywhere.  Each
colour is a colour picker; each size a bounded number; each shape/shadow/font a fixed choice.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.core.exceptions import ValidationError

from apps.core.forms import DesignSystemFormMixin, SectionedFormMixin
from apps.organizations.models import Organization
from apps.organizations.selectors import organizations_visible_to

from . import tokens as tk
from .constants import Appearance
from .models import Theme
from .policies import can_create_theme
from .presets import preset_choices
from .selectors import get_active_themes

SECTION_ORDER = (
    ("general", "General", "Name, description and the colour mode new users start in."),
    ("colors", "Colors", "Brand and status colours, for light and dark mode."),
    ("typography", "Typography", "Safe font stacks and text sizes."),
    ("shape", "Shape", "Corner radius scale and shadows."),
    ("density", "Density", "How compact or spacious the interface feels."),
    ("navigation", "Navigation", "The application navigation bar."),
    ("components", "Components", "Corner style of buttons, fields, cards, tables and dialogs."),
    ("light", "Light mode", "Page, card and text colours in light mode."),
    ("dark", "Dark mode", "Page, card and text colours in dark mode."),
)

_GROUP_TO_SECTION = {
    tk.BRAND: "colors",
    tk.STATUS: "colors",
    tk.TYPOGRAPHY: "typography",
    tk.SHAPE: "shape",
    tk.SHADOWS: "shape",
    tk.DENSITY: "density",
    tk.NAVIGATION: "navigation",
    tk.COMPONENTS: "components",
}


class ColorWidget(forms.TextInput):
    """A native colour picker (falls back to a text box in browsers without one)."""

    input_type = "color"
    manages_own_classes = True

    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        super().__init__({"class": "h-10 w-16 cursor-pointer rounded border border-border bg-surface p-1", **(attrs or {})})


class ColorField(forms.CharField):
    widget = ColorWidget

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)

    def clean(self, value: Any) -> str:
        value = super().clean(value)
        return tk.normalize_color(value) if value else ""


def field_name(section: str, key: str) -> str:
    return f"{section}__{key}"


def _build_token_field(token: tk.Token, initial: Any, section: str) -> forms.Field:
    common = {"required": False, "label": token.label, "help_text": token.help, "initial": initial}
    # Mode-specific tokens appear twice (light + dark); the accessible name says which.
    name = f"{token.label} ({section} mode)" if token.mode_specific else token.label
    if token.kind == "color":
        return ColorField(widget=ColorWidget(attrs={"aria-label": name}), **common)
    if token.kind in ("px", "number"):
        return forms.FloatField(
            min_value=token.minimum,
            max_value=token.maximum,
            widget=forms.NumberInput(
                attrs={"step": token.step, "min": token.minimum, "max": token.maximum, "aria-label": name}
            ),
            **common,
        )
    return forms.ChoiceField(choices=token.choices(), widget=forms.Select(attrs={"aria-label": name}), **common)


class ThemeCreateForm(DesignSystemFormMixin, forms.Form):
    """Start a new draft - blank or from a preset - then continue in the editor."""

    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), required=False)
    preset = forms.ChoiceField(choices=[], required=False, label="Start from")
    name = forms.CharField(max_length=120)
    description = forms.CharField(max_length=500, required=False)
    default_appearance = forms.ChoiceField(choices=Appearance.choices, initial=Appearance.SYSTEM)

    def __init__(self, *args: Any, user: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.user = user
        allowed = [o.pk for o in organizations_visible_to(user) if can_create_theme(user, o)]
        self.fields["organization"].queryset = Organization.objects.filter(pk__in=allowed)
        system_allowed = can_create_theme(user, None)
        self.fields["organization"].required = not system_allowed
        self.fields["organization"].empty_label = "System (application-wide)" if system_allowed else None
        self.fields["preset"].choices = [("", "Blank (built-in defaults)"), *preset_choices()]

    def clean_organization(self) -> Organization | None:
        organization = self.cleaned_data.get("organization")
        if not can_create_theme(self.user, organization):
            raise ValidationError("You are not allowed to create themes here.")
        return organization


class ThemeEditForm(DesignSystemFormMixin, SectionedFormMixin, forms.Form):
    """Edit a draft theme: general settings plus every design token."""

    name = forms.CharField(max_length=120)
    description = forms.CharField(max_length=500, required=False)
    default_appearance = forms.ChoiceField(choices=Appearance.choices)
    preview_mode = forms.ChoiceField(
        choices=[("light", "Light"), ("dark", "Dark")], initial="light", required=False, widget=forms.HiddenInput
    )

    def __init__(self, *args: Any, theme: Theme, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.theme = theme
        system_theme = get_active_themes(None)[0] if theme.organization_id else None
        self.system_layer = system_theme.tokens if system_theme else None
        self.base_tokens = tk.merge_tokens(self.system_layer)
        shown = tk.merge_tokens(system_theme.tokens if system_theme else None, theme.tokens)
        self.fields["name"].initial = theme.name
        self.fields["description"].initial = theme.description
        self.fields["default_appearance"].initial = theme.default_appearance
        self._token_fields: list[tuple[str, str, tk.Token]] = []
        for token in tk.TOKENS.values():
            sections = tk.MODES if token.mode_specific else (tk.SHARED,)
            for section in sections:
                name = field_name(section, token.key)
                self.fields[name] = _build_token_field(token, shown[section][token.key], section)
                self._token_fields.append((name, section, token))

    # --- tokens -----------------------------------------------------------------------
    def _collect(self, *, valid_only: bool) -> dict[str, dict[str, Any]]:
        """Overrides from the submitted values: only those that differ from the inherited base."""
        data = self.cleaned_data if hasattr(self, "cleaned_data") else {}
        overrides: dict[str, dict[str, Any]] = {}
        for name, section, token in self._token_fields:
            if name not in data:
                continue
            value = data[name]
            if value in ("", None):
                continue
            normalized = tk.validate_token_value(token.key, value)
            if normalized != self.base_tokens[section][token.key]:
                overrides.setdefault(section, {})[token.key] = normalized
        return overrides

    def token_overrides(self) -> dict[str, dict[str, Any]]:
        return self._collect(valid_only=False)

    def partial_tokens(self) -> dict[str, dict[str, Any]]:
        """Overrides from the fields that *are* valid - what a live preview can still show."""
        return self._collect(valid_only=True)

    def effective_tokens(self) -> dict[str, dict[str, Any]]:
        """The complete token set this (possibly unsaved) form would produce."""
        if not self.is_bound:
            return tk.merge_tokens(self.system_layer, self.theme.tokens)
        self.is_valid()  # populates cleaned_data with the valid fields
        return tk.merge_tokens(self.system_layer, self.partial_tokens())

    @property
    def preview_selected_mode(self) -> str:
        mode = self.data.get("preview_mode") if self.is_bound else None
        return mode if mode in tk.MODES else "light"

    # --- layout ---------------------------------------------------------------------------
    @property
    def editor_sections(self) -> list[dict[str, Any]]:
        """Sections -> rows -> (caption, bound field) cells, for the editor template."""
        sections: list[dict[str, Any]] = []
        for key, title, description in SECTION_ORDER:
            rows: list[dict[str, Any]] = []
            if key == "general":
                rows = [
                    {"label": self[n].label, "help": self[n].help_text, "cells": [("", self[n])]}
                    for n in ("name", "description", "default_appearance")
                ]
            else:
                rows = self._token_rows(key)
            sections.append({"id": key, "title": title, "description": description, "rows": rows})
        return sections

    def _token_rows(self, section_key: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for token in tk.TOKENS.values():
            if token.mode_specific:
                if section_key in tk.MODES:
                    if token.group != tk.SURFACE:
                        continue
                    cells = [("", self[field_name(section_key, token.key)])]
                elif _GROUP_TO_SECTION.get(token.group) == section_key:
                    cells = [
                        ("Light", self[field_name("light", token.key)]),
                        ("Dark", self[field_name("dark", token.key)]),
                    ]
                else:
                    continue
            elif _GROUP_TO_SECTION.get(token.group) == section_key:
                cells = [("", self[field_name(tk.SHARED, token.key)])]
            else:
                continue
            rows.append({"label": token.label, "help": token.help, "cells": cells})
        return rows


class ThemeCloneForm(DesignSystemFormMixin, forms.Form):
    name = forms.CharField(max_length=120)
    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), required=False)

    def __init__(self, *args: Any, user: Any, theme: Theme, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.user = user
        allowed = [o.pk for o in organizations_visible_to(user) if can_create_theme(user, o)]
        self.fields["organization"].queryset = Organization.objects.filter(pk__in=allowed)
        system_allowed = can_create_theme(user, None)
        self.fields["organization"].required = not system_allowed
        self.fields["organization"].empty_label = "System (application-wide)" if system_allowed else None
        self.fields["name"].initial = f"{theme.name} (copy)"
        self.fields["organization"].initial = theme.organization_id

    def clean_organization(self) -> Organization | None:
        organization = self.cleaned_data.get("organization")
        if not can_create_theme(self.user, organization):
            raise ValidationError("You are not allowed to create themes there.")
        return organization


class ThemeImportForm(DesignSystemFormMixin, forms.Form):
    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), required=False)
    file = forms.FileField(label="Theme file (.json)")

    def __init__(self, *args: Any, user: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.user = user
        allowed = [o.pk for o in organizations_visible_to(user) if can_create_theme(user, o)]
        self.fields["organization"].queryset = Organization.objects.filter(pk__in=allowed)
        system_allowed = can_create_theme(user, None)
        self.fields["organization"].required = not system_allowed
        self.fields["organization"].empty_label = "System (application-wide)" if system_allowed else None

    def clean_organization(self) -> Organization | None:
        organization = self.cleaned_data.get("organization")
        if not can_create_theme(self.user, organization):
            raise ValidationError("You are not allowed to import themes here.")
        return organization


class AppearanceForm(forms.Form):
    mode = forms.ChoiceField(choices=Appearance.choices, required=False)
