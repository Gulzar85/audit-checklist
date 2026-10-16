"""``/api/v1/location-types/`` and ``/api/v1/locations/`` - the location API.

Like the organization API this is a thin adapter over the selector, policy and
service layers, so the web UI and the API obey the same organization boundaries
and the same hierarchy rules.  There is no DELETE: locations and types are
deactivated, never removed (ADR 0014).
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.api.permissions import IsActiveUser, PolicyPermission
from apps.locations import services
from apps.locations.filters import LocationFilter, LocationTypeFilter
from apps.locations.policies import (
    can_activate_location,
    can_create_any_location_type,
    can_create_location,
    can_create_location_type,
    can_deactivate_location,
    can_edit_location,
    can_edit_location_type,
    can_move_location,
    can_toggle_location_type,
)
from apps.locations.selectors import (
    get_location_tree,
    location_types_visible_to,
    locations_visible_to,
)
from apps.locations.serializers import (
    LocationDeactivateSerializer,
    LocationMoveSerializer,
    LocationSerializer,
    LocationTreeNodeSerializer,
    LocationTypeSerializer,
    serialize_tree,
)

_READ_METHODS = ("GET", "HEAD", "OPTIONS")


class LocationTypeViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Location types in the caller's organizations, plus platform-defined types."""

    serializer_class = LocationTypeSerializer
    permission_classes = [IsActiveUser, PolicyPermission]
    filterset_class = LocationTypeFilter
    search_fields = ("name", "code")
    ordering_fields = ("name", "code", "sort_order", "created_at")
    ordering = ("sort_order", "name")

    def get_queryset(self):  # noqa: ANN201
        return location_types_visible_to(self.request.user).select_related("organization")

    def policy_allows_collection(self, request: Request) -> bool:
        if self.action == "create":
            return can_create_any_location_type(request.user)
        return True

    def policy_allows_object(self, request: Request, obj) -> bool:  # noqa: ANN001
        if self.action in ("activate", "deactivate"):
            return can_toggle_location_type(request.user, obj)
        if request.method in _READ_METHODS:
            return True
        return can_edit_location_type(request.user, obj)

    def perform_create(self, serializer) -> None:  # noqa: ANN001
        data = dict(serializer.validated_data)
        # The collection check only proves the caller can create *some* type; this
        # one is for a specific organization (or platform-wide when it is null).
        if not can_create_location_type(self.request.user, data.get("organization")):
            raise PermissionDenied
        serializer.instance = services.create_location_type(actor=self.request.user, **data)

    def perform_update(self, serializer) -> None:  # noqa: ANN001
        serializer.instance = services.update_location_type(
            serializer.instance, actor=self.request.user, **serializer.validated_data
        )

    @extend_schema(request=None, responses=LocationTypeSerializer)
    @action(detail=True, methods=["post"])
    def activate(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        location_type = services.activate_location_type(self.get_object(), actor=request.user)
        return Response(self.get_serializer(location_type).data)

    @extend_schema(request=None, responses=LocationTypeSerializer)
    @action(detail=True, methods=["post"])
    def deactivate(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        location_type = services.deactivate_location_type(self.get_object(), actor=request.user)
        return Response(self.get_serializer(location_type).data)


class LocationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Locations in the caller's organizations."""

    serializer_class = LocationSerializer
    permission_classes = [IsActiveUser, PolicyPermission]
    filterset_class = LocationFilter
    search_fields = ("name", "code", "city")
    ordering_fields = ("name", "code", "city", "created_at")
    ordering = ("name", "code")

    def get_queryset(self):  # noqa: ANN201
        return (
            locations_visible_to(self.request.user)
            .select_related("organization", "location_type", "parent")
            .with_children_count()
        )

    # -- authorization (policy layer) ---------------------------------------
    def policy_allows_collection(self, request: Request) -> bool:
        if self.action == "create":
            return can_create_location(request.user)
        return True

    def policy_allows_object(self, request: Request, obj) -> bool:  # noqa: ANN001
        if self.action == "activate":
            return can_activate_location(request.user, obj)
        if self.action == "deactivate":
            return can_deactivate_location(request.user, obj)
        if self.action == "move":
            return can_edit_location(request.user, obj)
        if request.method in _READ_METHODS:
            return True
        return can_edit_location(request.user, obj)

    # -- writes (service layer) ----------------------------------------------
    def perform_create(self, serializer) -> None:  # noqa: ANN001
        data = dict(serializer.validated_data)
        if not can_create_location(self.request.user, data["organization"], data.get("parent")):
            raise PermissionDenied
        serializer.instance = services.create_location(actor=self.request.user, **data)

    def perform_update(self, serializer) -> None:  # noqa: ANN001
        data = dict(serializer.validated_data)
        location = serializer.instance
        if "parent" in data and (data["parent"].pk if data["parent"] else None) != location.parent_id:
            if not can_move_location(self.request.user, location, data["parent"]):
                raise PermissionDenied
        serializer.instance = services.update_location(location, actor=self.request.user, **data)

    def _respond(self, location) -> Response:  # noqa: ANN001
        """Re-read through the scoped queryset so the response carries annotations."""
        fresh = self.get_queryset().get(pk=location.pk)
        return Response(self.get_serializer(fresh).data)

    @extend_schema(request=LocationMoveSerializer, responses=LocationSerializer)
    @action(detail=True, methods=["post"])
    def move(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        """Re-parent the location (``parent: null`` makes it a root)."""
        location = self.get_object()
        payload = LocationMoveSerializer(data=request.data, context=self.get_serializer_context())
        payload.is_valid(raise_exception=True)
        new_parent = payload.validated_data["parent"]
        if not can_move_location(request.user, location, new_parent):
            raise PermissionDenied
        return self._respond(services.move_location(location, new_parent, actor=request.user))

    @extend_schema(request=None, responses=LocationSerializer)
    @action(detail=True, methods=["post"])
    def activate(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._respond(services.activate_location(self.get_object(), actor=request.user))

    @extend_schema(request=LocationDeactivateSerializer, responses=LocationSerializer)
    @action(detail=True, methods=["post"])
    def deactivate(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        """Deactivate; ``cascade=true`` also deactivates the active descendants."""
        location = self.get_object()
        payload = LocationDeactivateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return self._respond(
            services.deactivate_location(
                location, actor=request.user, cascade=payload.validated_data["cascade"]
            )
        )

    @extend_schema(responses=LocationTreeNodeSerializer(many=True))
    @action(detail=False, methods=["get"], pagination_class=None)
    def tree(self, request: Request) -> Response:
        """The hierarchy as nested nodes, honouring the same filters as the list.

        A constant number of queries whatever the depth.  When filters narrow the
        result, ancestors of matches are included with ``matched: false`` so each
        match keeps its place in the tree.
        """
        queryset = self.filter_queryset(self.get_queryset().order_by())
        return Response(serialize_tree(get_location_tree(queryset, within=locations_visible_to(request.user))))
