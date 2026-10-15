"""``/api/v1/me/`` and ``/api/v1/users/`` - the identity API.

A thin adapter like the other v1 resources: the queryset is the selector
(``users_visible_to`` - the same tenant isolation as the web UI), authorization
is the policy layer, writes are the service layer.  The API is authenticated with
the session (same Django user); Microsoft tokens are never exposed or accepted
(ADR 0033).
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.selectors import users_visible_to
from apps.api.permissions import IsActiveUser, PolicyPermission

User = get_user_model()


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------
class IdentitySerializer(serializers.ModelSerializer):
    """The caller's own identity.

    Only self-service-editable fields are writable; Entra-owned fields (name,
    email, job title) and lifecycle/staff state are read-only.
    """

    is_entra_linked = serializers.BooleanField(read_only=True)
    can_sign_in = serializers.BooleanField(read_only=True)
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "display_name",
            "first_name",
            "last_name",
            "job_title",
            "phone_number",
            "avatar_url",
            "status",
            "is_staff",
            "is_entra_linked",
            "can_sign_in",
            "last_login",
            "date_joined",
        )
        read_only_fields = (
            "id",
            "email",
            "display_name",
            "first_name",
            "last_name",
            "job_title",
            "status",
            "is_staff",
            "last_login",
            "date_joined",
        )

    def get_avatar_url(self, obj: Any) -> str | None:
        return obj.avatar.url if obj.avatar else None


class UserSummarySerializer(serializers.ModelSerializer):
    """A tenant-scoped user summary (no sensitive identity data)."""

    is_entra_linked = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "display_name", "job_title", "status", "is_entra_linked")


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
class MeView(APIView):
    """Read and lightly edit the caller's own identity."""

    permission_classes = (IsAuthenticated, IsActiveUser)

    @extend_schema(responses=IdentitySerializer)
    def get(self, request: Request) -> Response:
        return Response(IdentitySerializer(request.user).data)

    @extend_schema(request=IdentitySerializer, responses=IdentitySerializer)
    def patch(self, request: Request) -> Response:
        serializer = IdentitySerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class UserViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """List and retrieve users the caller may see (search + filters)."""

    permission_classes = (IsAuthenticated, IsActiveUser, PolicyPermission)

    def get_queryset(self) -> Any:
        params = self.request.query_params
        queryset = users_visible_to(self.request.user)

        organization_id = params.get("organization")
        if organization_id:
            queryset = queryset.filter(
                organization_memberships__organization_id=organization_id,
                organization_memberships__is_active=True,
            )
        status = params.get("status")
        if status:
            queryset = queryset.filter(status=status)
        search = params.get("search")
        if search:
            queryset = queryset.filter(
                Q(email__icontains=search)
                | Q(display_name__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )
        return queryset.distinct().order_by("email")

    def get_serializer_class(self) -> Any:
        return UserSummarySerializer
