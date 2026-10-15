"""Serializers for the location domain (the API v1 representation).

No business logic lives here: writes go through the service layer, which applies
the hierarchy rules, locking, history and activity logging.  Relation fields are
scoped to what the requester may see, so a foreign organization's location, type
or organization is indistinguishable from a non-existent one.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.organizations.serializers import VisibleOrganizationField

from .models import Location, LocationType
from .selectors import TreeNode, location_types_visible_to, locations_visible_to


class VisibleLocationField(serializers.PrimaryKeyRelatedField):
    """A location reference restricted to the requester's visible locations."""

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        kwargs.setdefault("queryset", Location.objects.none())
        super().__init__(**kwargs)

    def get_queryset(self):  # noqa: ANN201
        return locations_visible_to(self.context["request"].user)


class VisibleLocationTypeField(serializers.PrimaryKeyRelatedField):
    """A location-type reference restricted to visible organizations' + platform types."""

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        kwargs.setdefault("queryset", LocationType.objects.none())
        super().__init__(**kwargs)

    def get_queryset(self):  # noqa: ANN201
        return location_types_visible_to(self.context["request"].user)


class LocationTypeSerializer(serializers.ModelSerializer):
    """A location type.  ``is_active`` changes via the activate/deactivate actions."""

    organization = VisibleOrganizationField(required=False, allow_null=True)
    is_platform_type = serializers.BooleanField(read_only=True)

    class Meta:
        model = LocationType
        fields = (
            "id",
            "organization",
            "is_platform_type",
            "name",
            "code",
            "description",
            "sort_order",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "is_active", "created_at", "updated_at")
        # Uniqueness is checked by the service against the normalised code.
        validators: list = []
        extra_kwargs = {"code": {"validators": []}}


class LocationSerializer(serializers.ModelSerializer):
    """A location.  ``is_active`` changes via the activate/deactivate actions."""

    organization = VisibleOrganizationField()
    location_type = VisibleLocationTypeField()
    parent = VisibleLocationField(required=False, allow_null=True)
    organization_code = serializers.CharField(source="organization.code", read_only=True)
    location_type_name = serializers.CharField(source="location_type.name", read_only=True)
    parent_name = serializers.CharField(source="parent.name", read_only=True, default=None)
    children_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Location
        fields = (
            "id",
            "organization",
            "organization_code",
            "location_type",
            "location_type_name",
            "parent",
            "parent_name",
            "name",
            "code",
            "description",
            "is_active",
            "children_count",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "country",
            "latitude",
            "longitude",
            "timezone",
            "metadata",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "is_active", "created_at", "updated_at")
        validators: list = []
        # Normalised by the model (``Location.clean_fields``); validated by the service.
        extra_kwargs = {"code": {"validators": []}, "country": {"validators": []}}


class LocationMoveSerializer(serializers.Serializer):
    """Input for ``POST /locations/{id}/move/`` (``parent: null`` makes it a root)."""

    parent = VisibleLocationField(allow_null=True)


class LocationDeactivateSerializer(serializers.Serializer):
    """Input for ``POST /locations/{id}/deactivate/``."""

    cascade = serializers.BooleanField(required=False, default=False)


class LocationTreeNodeSerializer(serializers.Serializer):
    """Schema of one node returned by ``GET /locations/tree/`` (children nest to any depth)."""

    id = serializers.UUIDField()
    organization = serializers.UUIDField()
    name = serializers.CharField()
    code = serializers.CharField()
    location_type = serializers.UUIDField()
    location_type_name = serializers.CharField()
    parent = serializers.UUIDField(allow_null=True)
    is_active = serializers.BooleanField()
    matched = serializers.BooleanField()
    children = serializers.ListField(child=serializers.JSONField())


def serialize_tree(roots: list[TreeNode]) -> list[dict[str, Any]]:
    """Turn assembled :class:`TreeNode` objects into nested dicts.

    Iterative (not recursive) so an unusually deep hierarchy cannot exhaust the
    Python stack.
    """
    result: list[dict[str, Any]] = []
    stack: list[tuple[TreeNode, list[dict[str, Any]]]] = [(node, result) for node in reversed(roots)]
    while stack:
        node, bucket = stack.pop()
        location = node.location
        item: dict[str, Any] = {
            "id": location.pk,
            "organization": location.organization_id,
            "name": location.name,
            "code": location.code,
            "location_type": location.location_type_id,
            "location_type_name": location.location_type.name,
            "parent": location.parent_id,
            "is_active": location.is_active,
            "matched": node.matched,
            "children": [],
        }
        bucket.append(item)
        stack.extend((child, item["children"]) for child in reversed(node.children))
    return result
