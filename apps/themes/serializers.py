"""Serializers for the theme API (v1 representation).

They shape and validate *input*; business rules live in ``services.py`` and the token
allow-list in ``tokens.py`` - so the API accepts exactly what the web editor accepts.
There is no field anywhere that holds CSS, HTML or JavaScript.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.organizations.models import Organization
from apps.organizations.selectors import organizations_visible_to

from . import tokens as tk
from .constants import PREVIEW_MODES, Appearance
from .models import Theme
from .presets import preset_choices


class VisibleOrganizationField(serializers.PrimaryKeyRelatedField):
    """Organizations the requester can see; unknown and foreign ids fail identically."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("queryset", Organization.objects.none())
        super().__init__(**kwargs)

    def get_queryset(self):  # noqa: ANN201
        return organizations_visible_to(self.context["request"].user)


class TokensField(serializers.JSONField):
    """A token override document, validated against the registry (unknown tokens are errors)."""

    def to_internal_value(self, data: Any) -> dict:
        value = super().to_internal_value(data)
        try:
            return tk.validate_tokens(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict.get("tokens") or exc.messages) from exc


class ThemeSerializer(serializers.ModelSerializer):
    """Read representation of a theme."""

    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = Theme
        fields = (
            "id", "organization", "name", "code", "description", "scope", "status", "is_active",
            "version", "default_appearance", "source_preset", "tokens",
            "published_at", "activated_at", "created_at", "updated_at",
        )
        read_only_fields = fields


class ThemeCreateSerializer(serializers.Serializer):
    organization = VisibleOrganizationField(required=False, allow_null=True)
    name = serializers.CharField(max_length=120)
    code = serializers.CharField(max_length=48, required=False, allow_blank=True)
    description = serializers.CharField(max_length=500, required=False, allow_blank=True)
    preset = serializers.ChoiceField(choices=preset_choices(), required=False, allow_blank=True)
    default_appearance = serializers.ChoiceField(choices=Appearance.choices, required=False)
    tokens = TokensField(required=False)


class ThemeUpdateSerializer(serializers.Serializer):
    """Drafts only.  ``tokens`` *replaces* the theme's override document.

    Fields that are fixed after creation (``code``, ``organization``, ``status`` ...) are
    rejected loudly instead of being silently ignored.
    """

    name = serializers.CharField(max_length=120, required=False)
    description = serializers.CharField(max_length=500, required=False, allow_blank=True)
    default_appearance = serializers.ChoiceField(choices=Appearance.choices, required=False)
    tokens = TokensField(required=False)

    def to_internal_value(self, data: Any) -> Any:
        if isinstance(data, dict):
            fixed = {key: "This field cannot be changed." for key in data if key not in self.fields}
            if fixed:
                raise serializers.ValidationError(fixed)
        return super().to_internal_value(data)


class ThemeCloneSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    organization = VisibleOrganizationField(required=False, allow_null=True)


class ThemePreviewSerializer(serializers.Serializer):
    """Preview a token override document *without saving anything*."""

    tokens = TokensField(required=False)
    mode = serializers.ChoiceField(choices=PREVIEW_MODES, default="light")


class ThemeImportSerializer(serializers.Serializer):
    organization = VisibleOrganizationField(required=False, allow_null=True)
    document = serializers.JSONField()


class AppearanceSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=Appearance.choices, allow_null=True)
