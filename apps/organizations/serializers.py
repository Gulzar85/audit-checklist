"""Serializers for the organization domain (the API v1 representation).

Serializers shape and validate *input*; they contain no business rules.  Writes
are performed by the service layer (see ``apps/api/v1/organizations.py``), which
re-validates with the model, so the API enforces exactly what the web UI does.
"""

from __future__ import annotations

from rest_framework import serializers

from .models import Organization
from .selectors import organizations_visible_to


class VisibleOrganizationField(serializers.PrimaryKeyRelatedField):
    """An organization reference restricted to organizations the requester can see.

    An organization that exists but is not visible produces the *same* error as
    one that does not exist, so a client cannot probe for other tenants.
    """

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        kwargs.setdefault("queryset", Organization.objects.none())
        super().__init__(**kwargs)

    def get_queryset(self):  # noqa: ANN201
        return organizations_visible_to(self.context["request"].user)


class OrganizationSerializer(serializers.ModelSerializer):
    """Read/write representation of an organization.

    ``status`` is read-only: lifecycle transitions are explicit operations
    (``POST /organizations/{id}/status/``).  ``code`` cannot change after
    creation; uniqueness of ``code``/``slug`` is checked by the service against
    their *normalised* forms, so DRF's case-sensitive unique validators are off.
    """

    is_active = serializers.BooleanField(read_only=True)
    logo = serializers.ImageField(required=False, allow_null=True)
    favicon = serializers.ImageField(required=False, allow_null=True)

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "code",
            "slug",
            "legal_name",
            "description",
            "status",
            "is_active",
            "logo",
            "favicon",
            "primary_color",
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
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "status", "created_at", "updated_at")
        # Fields the model *normalises* (``Organization.normalize``) are validated by
        # the service after normalisation - validating the raw input here would
        # reject ``country="gb"`` that the web UI happily accepts.
        extra_kwargs = {
            "code": {"required": False, "validators": []},
            "slug": {"required": False, "validators": []},
            "country": {"validators": []},
            "currency": {"validators": []},
            "language": {"validators": []},
            "primary_color": {"validators": []},
        }


class OrganizationStatusSerializer(serializers.Serializer):
    """Input for ``POST /organizations/{id}/status/``."""

    status = serializers.ChoiceField(choices=Organization.Status.choices)
