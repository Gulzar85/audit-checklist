"""``/api/v1/organizations/`` - the organization API.

A thin adapter: the queryset comes from the selector layer (the same isolation the
web UI uses), authorization from the policy layer, and writes from the service
layer.  There is deliberately no DELETE: organizations are deactivated, never
removed (ADR 0014).
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.api.permissions import IsActiveUser, PolicyPermission
from apps.organizations import services
from apps.organizations.filters import OrganizationFilter
from apps.organizations.policies import (
    can_change_organization_status,
    can_create_organization,
    can_manage_organization,
)
from apps.organizations.selectors import organizations_visible_to
from apps.organizations.serializers import OrganizationSerializer, OrganizationStatusSerializer

_FILE_FIELDS = ("logo", "favicon")


class OrganizationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """List, read, create and update organizations the caller may see."""

    serializer_class = OrganizationSerializer
    permission_classes = [IsActiveUser, PolicyPermission]
    filterset_class = OrganizationFilter
    search_fields = ("name", "code", "legal_name")
    ordering_fields = ("name", "code", "status", "created_at")
    ordering = ("name",)

    def get_queryset(self):  # noqa: ANN201
        return organizations_visible_to(self.request.user)

    # -- authorization (policy layer) ---------------------------------------
    def policy_allows_collection(self, request: Request) -> bool:
        if self.action == "create":
            return can_create_organization(request.user)
        return True

    def policy_allows_object(self, request: Request, obj) -> bool:  # noqa: ANN001
        if self.action == "status":
            return can_change_organization_status(request.user, obj)
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return can_manage_organization(request.user, obj)

    # -- writes (service layer) ----------------------------------------------
    def perform_create(self, serializer) -> None:  # noqa: ANN001
        serializer.instance = services.create_organization(
            actor=self.request.user, **serializer.validated_data
        )

    def perform_update(self, serializer) -> None:  # noqa: ANN001
        data = dict(serializer.validated_data)
        for name in _FILE_FIELDS:  # an explicit ``null`` clears an uploaded file
            if name in data and data[name] is None:
                data[name] = False
        serializer.instance = services.update_organization(
            serializer.instance, actor=self.request.user, **data
        )

    @extend_schema(request=OrganizationStatusSerializer, responses=OrganizationSerializer)
    @action(detail=True, methods=["post"])
    def status(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        """Activate, deactivate or suspend the organization (platform staff only)."""
        organization = self.get_object()
        payload = OrganizationStatusSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        organization = services.set_organization_status(
            organization, payload.validated_data["status"], actor=request.user
        )
        return Response(self.get_serializer(organization).data)
