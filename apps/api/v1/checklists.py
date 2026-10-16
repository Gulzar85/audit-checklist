"""``/api/v1/checklists/`` - checklist definitions.

A thin adapter, like every v1 resource: the queryset is ``selectors.templates_for_user`` (organization
isolation + ``checklist.view``), authorization is ``apps.checklists.policies`` (RBAC), writes are
``apps.checklists.services``.  Structure (sections, items, options) is exposed read-only here; it is
authored in the builder.  There is no DELETE: templates are archived.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response

from apps.api.permissions import IsActiveUser, PolicyPermission
from apps.checklists import constants as c
from apps.checklists import policies, selectors, services
from apps.checklists.filters import TemplateFilter
from apps.checklists.models import ChecklistCategory, ChecklistItem
from apps.checklists.serializers import (
    ChecklistItemSerializer,
    ChecklistSectionSerializer,
    ChecklistTemplateSerializer,
    ChecklistTemplateWriteSerializer,
    ChecklistVersionSerializer,
    DuplicateSerializer,
    NewVersionSerializer,
)
from apps.organizations.models import Organization
from apps.permissions.authorization import visible_organization_ids


def _raise(exc: DjangoValidationError) -> None:
    raise serializers.ValidationError(exc.message_dict if hasattr(exc, "error_dict") else exc.messages) from exc


class ChecklistViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ChecklistTemplateSerializer
    permission_classes = [IsActiveUser, PolicyPermission]
    filterset_class = TemplateFilter
    search_fields = ("name", "code", "description", "category__name")
    ordering_fields = ("name", "code", "status", "updated_at", "created_at")
    ordering = ("name", "code")

    def get_queryset(self):  # noqa: ANN201
        return selectors.templates_for_user(self.request.user)

    # -- authorization (policy layer) ---------------------------------------------------------
    def policy_allows_collection(self, request: Request) -> bool:
        if self.action == "create":
            return policies.can_create_template(request.user)
        return True

    def policy_allows_object(self, request: Request, obj) -> bool:  # noqa: ANN001
        if self.action in ("archive", "restore"):
            return policies.can_manage_template(request.user, obj)
        if self.action == "duplicate":
            return policies.can_duplicate_template(request.user, obj)
        if self.action == "new_version":
            return policies.can_create_version(request.user, obj)
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return policies.can_update_template(request.user, obj)  # update, activate

    def _fresh(self, template) -> Response:  # noqa: ANN001
        return Response(self.get_serializer(self.get_queryset().get(pk=template.pk)).data)

    # -- writes -----------------------------------------------------------------------------------
    @extend_schema(request=ChecklistTemplateWriteSerializer, responses=ChecklistTemplateSerializer)
    def create(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        payload = ChecklistTemplateWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        missing = {k: "This field is required." for k in ("organization", "code", "name") if k not in data}
        if missing:
            raise serializers.ValidationError(missing)
        organization = Organization.objects.filter(pk=data["organization"]).first()
        # An organization you cannot enter is "not found" (existence is not revealed); one you can enter
        # but may not create in is forbidden.
        if organization is None or organization.pk not in visible_organization_ids(request.user):
            raise NotFound("Organization not found.")
        if not policies.can_create_template(request.user, organization):
            raise PermissionDenied
        category = None
        if data.get("category"):
            category = ChecklistCategory.objects.filter(pk=data["category"], organization=organization).first()
            if category is None:
                raise serializers.ValidationError({"category": "Unknown category."})
        try:
            template = services.create_checklist_template(
                organization=organization, name=data["name"], code=data["code"], actor=request.user,
                description=data.get("description", ""), instructions=data.get("instructions", ""),
                estimated_duration_minutes=data.get("estimated_duration_minutes"), category=category,
            )
        except DjangoValidationError as exc:
            _raise(exc)
        return Response(self._fresh(template).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=ChecklistTemplateWriteSerializer, responses=ChecklistTemplateSerializer)
    def update(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        template = self.get_object()
        payload = ChecklistTemplateWriteSerializer(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        data = {k: v for k, v in payload.validated_data.items() if k in (
            "name", "description", "instructions", "estimated_duration_minutes", "category")}
        if "category" in data:
            category_id = data["category"]
            data["category"] = ChecklistCategory.objects.filter(pk=category_id, organization=template.organization_id).first() if category_id else None
            if category_id and data["category"] is None:
                raise serializers.ValidationError({"category": "Unknown category."})
        try:
            services.update_checklist_template(template, actor=request.user, **data)
        except DjangoValidationError as exc:
            _raise(exc)
        return self._fresh(template)

    def _transition(self, service) -> Response:  # noqa: ANN001
        template = self.get_object()
        try:
            service(template, actor=self.request.user)
        except DjangoValidationError as exc:
            _raise(exc)
        return self._fresh(template)

    @extend_schema(request=None, responses=ChecklistTemplateSerializer)
    @action(detail=True, methods=["post"])
    def archive(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(services.archive_checklist_template)

    @extend_schema(request=None, responses=ChecklistTemplateSerializer)
    @action(detail=True, methods=["post"])
    def restore(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(services.restore_checklist_template)

    @extend_schema(request=None, responses=ChecklistTemplateSerializer)
    @action(detail=True, methods=["post"])
    def activate(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(services.activate_checklist_template)

    @extend_schema(request=DuplicateSerializer, responses=ChecklistTemplateSerializer)
    @action(detail=True, methods=["post"])
    def duplicate(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        template = self.get_object()
        payload = DuplicateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            copy = services.duplicate_checklist_template(template, actor=request.user, **payload.validated_data)
        except DjangoValidationError as exc:
            _raise(exc)
        return Response(self._fresh(copy).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=NewVersionSerializer, responses=ChecklistVersionSerializer)
    @action(detail=True, methods=["post"], url_path="versions/new")
    def new_version(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        template = self.get_object()
        payload = NewVersionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            version = services.create_checklist_version(template, actor=request.user, **payload.validated_data)
        except DjangoValidationError as exc:
            _raise(exc)
        return Response(ChecklistVersionSerializer(version).data, status=status.HTTP_201_CREATED)

    # -- structure (read-only) ------------------------------------------------------------------------
    def _structure_version(self, template, request: Request):  # noqa: ANN001, ANN202
        number = request.query_params.get("version")
        if number:
            if not number.isdigit():
                raise serializers.ValidationError({"version": "Must be a version number."})
            version = selectors.get_version(template, int(number))
        else:
            version = (
                template.versions.filter(status=c.VersionStatus.PUBLISHED).order_by("-version_number").first()
                or template.versions.order_by("-version_number").first()
            )
        if version is None:
            raise NotFound("Version not found.")
        return version

    @extend_schema(responses=ChecklistVersionSerializer(many=True))
    @action(detail=True, methods=["get"], pagination_class=None)
    def versions(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return Response(ChecklistVersionSerializer(selectors.versions_of(self.get_object()), many=True).data)

    @extend_schema(responses=ChecklistSectionSerializer(many=True))
    @action(detail=True, methods=["get"], pagination_class=None)
    def sections(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        """Sections (with items and options) of ``?version=N`` (default: latest published, else latest)."""
        version = self._structure_version(self.get_object(), request)
        return Response(ChecklistSectionSerializer(selectors.get_sections_for_version(version), many=True).data)

    @extend_schema(responses=ChecklistItemSerializer(many=True))
    @action(detail=True, methods=["get"], pagination_class=None)
    def items(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        version = self._structure_version(self.get_object(), request)
        rows = (
            ChecklistItem.objects.filter(section__version=version)
            .order_by("section__sort_order", "sort_order").prefetch_related("options")
        )
        return Response(ChecklistItemSerializer(rows, many=True).data)
