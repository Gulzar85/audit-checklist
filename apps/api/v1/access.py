"""``/api/v1/roles/``, ``/permissions/``, ``/members/`` and ``/access/me/`` - the RBAC API.

A thin adapter over the same selectors, policies and services the web UI uses (there is no
separate API security model).  The organization is explicit - ``?organization=<id>`` or the
caller's default - and is *validated* against the organizations the caller may enter, so a forged
id is a 404, never data.  Authorization:

* ``HasApplicationPermission`` asks "does the caller hold it somewhere?" per action (default deny);
* the queryset is organization-scoped (out of scope -> 404);
* policies re-check the concrete object/scope on writes (visible but forbidden -> 403).
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.selectors import resolve_active_organization
from apps.api.permissions import HasApplicationPermission, IsActiveUser, OrganizationMembershipPermission
from apps.core.utils import parse_uuid
from apps.locations.models import Location
from apps.organizations.models import OrganizationMembership
from apps.organizations.selectors import organizations_visible_to
from apps.permissions import policies, registry, selectors, services
from apps.permissions.authorization import get_effective_permissions, has_permission
from apps.permissions.models import MembershipRole, Role

P = registry.P


def api_organization(request: Request) -> Any:
    """The organization an API call acts in (validated; forged ids 404)."""
    if hasattr(request, "_api_organization"):
        return request._api_organization
    raw = request.query_params.get("organization") or (
        request.data.get("organization") if hasattr(request.data, "get") else None
    )
    visible = organizations_visible_to(request.user)
    if raw:
        pk = parse_uuid(raw)
        organization = visible.filter(pk=pk).first() if pk else None
    else:
        organization = resolve_active_organization(request.user, None)
    if organization is None:
        raise NotFound("Organization not found.")
    request._api_organization = organization
    return organization


def _raise(exc: DjangoValidationError) -> None:
    raise serializers.ValidationError(exc.message_dict if hasattr(exc, "error_dict") else exc.messages) from exc


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------
class RoleSerializer(serializers.ModelSerializer):
    permissions = serializers.SerializerMethodField()
    permission_count = serializers.IntegerField(read_only=True, default=0)
    member_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Role
        fields = ("id", "organization", "name", "slug", "description", "is_system", "is_active",
                  "permissions", "permission_count", "member_count", "created_at", "updated_at")
        read_only_fields = fields

    def get_permissions(self, role: Role) -> list[str]:
        return sorted(policies.role_permission_codes(role))


class RoleWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    description = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    permissions = serializers.ListField(child=serializers.CharField(), required=False, default=list)

    def validate_permissions(self, codes: list[str]) -> list[str]:
        unknown = [code for code in codes if not registry.is_registered(code)]
        if unknown:
            raise serializers.ValidationError(f"Unknown permission: {', '.join(unknown)}")
        return codes


class PermissionSerializer(serializers.Serializer):
    code = serializers.CharField()
    resource = serializers.CharField()
    action = serializers.CharField()
    label = serializers.CharField()
    org_wide_only = serializers.BooleanField()


class AssignmentSerializer(serializers.ModelSerializer):
    role_name = serializers.CharField(source="role.name", read_only=True)
    role_slug = serializers.CharField(source="role.slug", read_only=True)

    class Meta:
        model = MembershipRole
        fields = ("id", "role", "role_name", "role_slug", "scope_type", "location", "is_active", "assigned_at")
        read_only_fields = fields


class AssignRoleSerializer(serializers.Serializer):
    role = serializers.UUIDField()
    scope_type = serializers.ChoiceField(choices=MembershipRole.ScopeType.choices,
                                         default=MembershipRole.ScopeType.ORGANIZATION)
    location = serializers.UUIDField(required=False, allow_null=True, default=None)


class MemberSerializer(serializers.ModelSerializer):
    user_id = serializers.UUIDField(read_only=True)
    email = serializers.CharField(source="user.email", read_only=True)
    display_name = serializers.CharField(source="user.display_name", read_only=True)
    roles = serializers.SerializerMethodField()

    class Meta:
        model = OrganizationMembership
        fields = ("id", "user_id", "email", "display_name", "status", "roles", "joined_at")
        read_only_fields = fields

    def get_roles(self, membership: OrganizationMembership) -> list[dict[str, Any]]:
        return [
            {"id": str(a.pk), "role": a.role.slug, "scope_type": a.scope_type,
             "location": str(a.location_id) if a.location_id else None}
            for a in getattr(membership, "active_assignments", [])
        ]


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
class PermissionListView(APIView):
    """The permission catalogue (``permission.view``)."""

    permission_classes = (IsAuthenticated, IsActiveUser, OrganizationMembershipPermission)

    @extend_schema(responses=PermissionSerializer(many=True))
    def get(self, request: Request) -> Response:
        organization = api_organization(request)
        if not policies.can_view_permissions(request.user, organization):
            raise PermissionDenied
        rows = [
            {"code": d.code, "resource": d.resource, "action": d.action, "label": d.label,
             "org_wide_only": d.org_wide_only}
            for d in registry.REGISTRY
        ]
        return Response(rows)


class RoleViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet
):
    """Roles available in the organization: its custom roles plus the platform (system) roles."""

    serializer_class = RoleSerializer
    permission_classes = (IsAuthenticated, IsActiveUser, OrganizationMembershipPermission, HasApplicationPermission)
    required_permissions = {
        "list": P.ROLE_VIEW, "retrieve": P.ROLE_VIEW, "create": P.ROLE_CREATE, "update_role": P.ROLE_UPDATE,
        "deactivate": P.ROLE_DEACTIVATE, "activate": P.ROLE_DEACTIVATE,
    }
    search_fields = ("name", "slug")
    ordering = ("-is_system", "name")

    def get_queryset(self):  # noqa: ANN201
        organization = api_organization(self.request)
        if not policies.can_view_roles(self.request.user, organization):
            raise PermissionDenied
        return selectors.roles_for_display(organization)

    def _role(self) -> Role:
        return self.get_object()

    @extend_schema(request=RoleWriteSerializer, responses=RoleSerializer)
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = api_organization(request)
        payload = RoleWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        if not policies.can_create_role(request.user, organization, data["permissions"]):
            raise PermissionDenied
        try:
            role = services.create_role(
                organization=organization, name=data["name"], description=data["description"],
                permissions=data["permissions"], actor=request.user,
            )
        except DjangoValidationError as exc:
            _raise(exc)
        fresh = selectors.roles_for_display(organization).get(pk=role.pk)
        return Response(self.get_serializer(fresh).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=RoleWriteSerializer, responses=RoleSerializer)
    @action(detail=True, methods=["put", "patch"], url_path="update", url_name="update-role")
    def update_role(self, request: Request, pk: Any = None) -> Response:
        """Rename a custom role and replace its permission set."""
        role = self._role()
        payload = RoleWriteSerializer(data=request.data, partial=request.method == "PATCH")
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        codes = data.get("permissions")
        if not policies.can_edit_role(request.user, role, codes):
            raise PermissionDenied
        try:
            if "name" in data or "description" in data:
                services.update_role(role, actor=request.user,
                                     **{k: v for k, v in data.items() if k in ("name", "description")})
            if codes is not None:
                services.set_role_permissions(role, codes, actor=request.user)
        except DjangoValidationError as exc:
            _raise(exc)
        return Response(self.get_serializer(self.get_queryset().get(pk=role.pk)).data)

    @extend_schema(request=None, responses=RoleSerializer)
    @action(detail=True, methods=["post"])
    def deactivate(self, request: Request, pk: Any = None) -> Response:
        role = self._role()
        if not policies.can_deactivate_role(request.user, role):
            raise PermissionDenied
        try:
            services.deactivate_role(role, actor=request.user)
        except DjangoValidationError as exc:
            _raise(exc)
        return Response(self.get_serializer(self.get_queryset().get(pk=role.pk)).data)

    @extend_schema(request=None, responses=RoleSerializer)
    @action(detail=True, methods=["post"])
    def activate(self, request: Request, pk: Any = None) -> Response:
        role = self._role()
        if not policies.can_deactivate_role(request.user, role):
            raise PermissionDenied
        try:
            services.activate_role(role, actor=request.user)
        except DjangoValidationError as exc:
            _raise(exc)
        return Response(self.get_serializer(self.get_queryset().get(pk=role.pk)).data)


class MemberViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Members of the organization with their role assignments."""

    serializer_class = MemberSerializer
    permission_classes = (IsAuthenticated, IsActiveUser, OrganizationMembershipPermission, HasApplicationPermission)
    required_permissions = {
        "list": P.USER_VIEW, "retrieve": P.USER_VIEW, "roles": P.USER_VIEW, "assign": P.USER_VIEW,
        "remove_role": P.USER_VIEW, "set_status": P.MEMBERSHIP_MANAGE,
    }

    def get_queryset(self):  # noqa: ANN201
        organization = api_organization(self.request)
        if not has_permission(self.request.user, P.USER_VIEW, organization=organization):
            raise PermissionDenied
        return selectors.members_for_display(organization)

    @extend_schema(responses=AssignmentSerializer(many=True))
    @action(detail=True, methods=["get"])
    def roles(self, request: Request, pk: Any = None) -> Response:
        membership = self.get_object()
        if not policies.can_view_access(request.user, membership):
            raise PermissionDenied
        rows = membership.role_assignments.filter(is_active=True).select_related("role")
        return Response(AssignmentSerializer(rows, many=True).data)

    @extend_schema(request=AssignRoleSerializer, responses=AssignmentSerializer)
    @action(detail=True, methods=["post"], url_path="roles/assign")
    def assign(self, request: Request, pk: Any = None) -> Response:
        membership = self.get_object()
        payload = AssignRoleSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        role = Role.objects.filter(pk=data["role"]).first()
        if role is None or role.organization_id not in (None, membership.organization_id):
            raise NotFound("Role not found.")  # another tenant's role is never revealed
        location = None
        if data["scope_type"] != MembershipRole.ScopeType.ORGANIZATION:
            location = Location.objects.filter(pk=data["location"], organization_id=membership.organization_id).first()
            if location is None:
                raise serializers.ValidationError({"location": "Choose a location in this organization."})
        if not policies.can_assign_role(request.user, membership, role, scope_type=data["scope_type"], location=location):
            raise PermissionDenied
        try:
            assignment = services.assign_role(
                membership, role, scope_type=data["scope_type"], location=location, actor=request.user
            )
        except DjangoValidationError as exc:
            _raise(exc)
        return Response(AssignmentSerializer(assignment).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses=None)
    @action(detail=True, methods=["delete"], url_path=r"roles/(?P<assignment_id>[0-9a-f-]{36})")
    def remove_role(self, request: Request, pk: Any = None, assignment_id: str = "") -> Response:
        membership = self.get_object()
        assignment = membership.role_assignments.filter(pk=assignment_id, is_active=True).select_related("role").first()
        if assignment is None:
            raise NotFound("Assignment not found.")
        if not policies.can_remove_role(request.user, assignment):
            raise PermissionDenied
        services.remove_role(assignment, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=dict, responses=MemberSerializer)
    @action(detail=True, methods=["post"], url_path="status")
    def set_status(self, request: Request, pk: Any = None) -> Response:
        membership = self.get_object()
        if not policies.can_manage_membership_status(request.user, membership):
            raise PermissionDenied
        value = request.data.get("status")
        if value not in OrganizationMembership.Status.values:
            raise serializers.ValidationError({"status": "Unknown status."})
        try:
            services.change_membership_status(membership, value, actor=request.user)
        except DjangoValidationError as exc:
            _raise(exc)
        return Response(MemberSerializer(self.get_queryset().get(pk=membership.pk)).data)


class MyAccessView(APIView):
    """``/api/v1/access/me/`` - what the caller may do in the organization, and where."""

    permission_classes = (IsAuthenticated, IsActiveUser, OrganizationMembershipPermission)

    @extend_schema(responses=dict)
    def get(self, request: Request) -> Response:
        organization = api_organization(request)
        effective = get_effective_permissions(request.user, organization)
        return Response({
            "organization": str(organization.pk),
            "permissions": {
                code: {"organization": grant["organization"], "locations": grant["locations"]}
                for code, grant in effective.items()
            },
        })
