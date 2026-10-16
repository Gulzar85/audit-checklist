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
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.permissions import IsActiveUser, PolicyPermission
from apps.checklists import constants as c
from apps.checklists import policies, selectors, services
from apps.checklists.filters import TemplateFilter
from apps.checklists.models import ChecklistCategory, ChecklistItem, ChecklistRule
from apps.checklists.answer_types import registry
from apps.checklists.answer_types.serializers import AnswerTypeDetailSerializer, AnswerTypeSerializer
from apps.checklists.rules import (
    RuleSpec,
    all_limits,
    compile_rules,
    describe_actions,
    describe_operators,
    evaluate,
    snapshot_for_version,
    validate_rule_set,
)
from apps.checklists.serializers import (
    ChecklistItemSerializer,
    ChecklistItemWriteSerializer,
    ChecklistRuleSerializer,
    RuleValidateSerializer,
    RuleWriteSerializer,
    SimulationSerializer,
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
    detail = exc.message_dict if hasattr(exc, "error_dict") else exc.messages
    if hasattr(exc, "issues") and isinstance(detail, dict):  # rule validation: add the structured findings
        detail = {**detail, "issues": [i.as_dict() for i in exc.issues]}
    raise serializers.ValidationError(detail) from exc


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
        if self.action in ("rules_simulate", "rules_compiled"):  # read-only: viewing the checklist is enough
            return True
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

    @extend_schema(request=ChecklistItemWriteSerializer, responses=ChecklistItemSerializer(many=True))
    @action(detail=True, methods=["get", "post"], pagination_class=None)
    def items(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        """GET: the items of ``?version=N``.  POST: add an item to a section of the draft version."""
        template = self.get_object()
        if request.method == "POST":
            return self._create_item(template, request)
        version = self._structure_version(template, request)
        rows = (
            ChecklistItem.objects.filter(section__version=version)
            .order_by("section__sort_order", "sort_order").prefetch_related("options")
        )
        return Response(ChecklistItemSerializer(rows, many=True).data)

    @extend_schema(request=ChecklistItemWriteSerializer, responses=ChecklistItemSerializer)
    @action(detail=True, methods=["get", "patch", "delete"], url_path=r"items/(?P<item_pk>[0-9a-f-]{36})")
    def item_detail(self, request: Request, pk=None, item_pk: str = "") -> Response:  # noqa: ANN001
        template = self.get_object()
        if request.method == "GET":
            item = (ChecklistItem.objects.filter(section__version__template=template, pk=item_pk)
                    .select_related("section").prefetch_related("options").first())
            if item is None:
                raise NotFound("Item not found.")
            return Response(ChecklistItemSerializer(item).data)
        draft = self._draft_for_edit(template, request)
        item = selectors.get_item(draft, item_pk)
        if item is None:  # not in the editable draft (another version, template or tenant)
            raise NotFound("Item not found in the draft version.")
        try:
            if request.method == "DELETE":
                services.delete_item(item, actor=request.user)
                return Response(status=status.HTTP_204_NO_CONTENT)
            payload = ChecklistItemWriteSerializer(data=request.data, partial=True)
            payload.is_valid(raise_exception=True)
            data = dict(payload.validated_data)
            data.pop("section", None)  # items are not moved between sections here
            options = data.pop("options", None)
            if data:
                services.update_item(item, actor=request.user, **data)
            if options is not None:
                item.refresh_from_db()
                services.set_options(item, options, actor=request.user)
        except DjangoValidationError as exc:
            _raise(exc)
        fresh = ChecklistItem.objects.filter(pk=item.pk).prefetch_related("options").get()
        return Response(ChecklistItemSerializer(fresh).data)

    def _draft_for_edit(self, template, request: Request):  # noqa: ANN001, ANN202
        draft = selectors.get_draft_version(template)
        if draft is None:
            raise serializers.ValidationError({"version": "This checklist has no draft version. Create one first."})
        if not policies.can_edit_version(request.user, draft):
            raise PermissionDenied
        return draft

    def _create_item(self, template, request: Request) -> Response:  # noqa: ANN001
        draft = self._draft_for_edit(template, request)
        payload = ChecklistItemWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        missing = {k: "This field is required." for k in ("section", "question") if k not in data}
        if missing:
            raise serializers.ValidationError(missing)
        section = selectors.get_section(draft, data.pop("section"))
        if section is None:
            raise serializers.ValidationError({"section": "Unknown section in the draft version."})
        options = data.pop("options", ())
        try:
            item = services.create_item(section, actor=request.user, options=options, **data)
        except DjangoValidationError as exc:
            _raise(exc)
        fresh = ChecklistItem.objects.filter(pk=item.pk).prefetch_related("options").get()
        return Response(ChecklistItemSerializer(fresh).data, status=status.HTTP_201_CREATED)


    # -- rules (Phase 6) -------------------------------------------------------------------------------------------
    def _rules_version(self, template, request: Request):  # noqa: ANN001, ANN202
        """The version a rules request is about: ``?version=N``/body ``version``, else the draft, else the latest published."""
        number = request.query_params.get("version") or (request.data.get("version") if hasattr(request.data, "get") else None)
        if number not in (None, ""):
            if not str(number).isdigit():
                raise serializers.ValidationError({"version": "Must be a version number."})
            version = selectors.get_version(template, int(number))
        else:
            version = selectors.get_draft_version(template) or (
                template.versions.filter(status=c.VersionStatus.PUBLISHED).order_by("-version_number").first())
        if version is None:
            raise NotFound("Version not found.")
        return version

    @extend_schema(request=RuleWriteSerializer, responses=ChecklistRuleSerializer(many=True))
    @action(detail=True, methods=["get", "post"], pagination_class=None)
    def rules(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        """GET: the rules of a version (with readable summaries).  POST: add a rule to the draft version."""
        template = self.get_object()
        if request.method == "POST":
            draft = self._draft_for_edit(template, request)
            payload = RuleWriteSerializer(data=request.data)
            payload.is_valid(raise_exception=True)
            data = dict(payload.validated_data)
            if "name" not in data or "actions" not in data:
                raise serializers.ValidationError({k: "This field is required." for k in ("name", "actions") if k not in data})
            try:
                rule = services.create_rule(draft, actor=request.user, **data)
            except DjangoValidationError as exc:
                _raise(exc)
            snapshot = snapshot_for_version(draft)
            return Response(ChecklistRuleSerializer(rule, context={"snapshot": snapshot}).data, status=status.HTTP_201_CREATED)
        version = self._rules_version(template, request)
        snapshot = snapshot_for_version(version)
        rows = selectors.rules_for_version(version).select_related("version")
        return Response(ChecklistRuleSerializer(rows, many=True, context={"snapshot": snapshot}).data)

    @extend_schema(request=RuleWriteSerializer, responses=ChecklistRuleSerializer)
    @action(detail=True, methods=["get", "patch", "delete"], url_path=r"rules/(?P<rule_pk>[0-9a-f-]{36})")
    def rule_detail(self, request: Request, pk=None, rule_pk: str = "") -> Response:  # noqa: ANN001
        template = self.get_object()
        if request.method == "GET":
            rule = ChecklistRule.objects.filter(version__template=template, pk=rule_pk).select_related("version").first()
            if rule is None:
                raise NotFound("Rule not found.")
            return Response(ChecklistRuleSerializer(rule, context={"snapshot": snapshot_for_version(rule.version)}).data)
        draft = self._draft_for_edit(template, request)
        rule = selectors.get_rule(draft, rule_pk)
        if rule is None:  # another version, template or tenant: never revealed
            raise NotFound("Rule not found in the draft version.")
        try:
            if request.method == "DELETE":
                services.delete_rule(rule, actor=request.user)
                return Response(status=status.HTTP_204_NO_CONTENT)
            payload = RuleWriteSerializer(data=request.data, partial=True)
            payload.is_valid(raise_exception=True)
            data = dict(payload.validated_data)
            data.pop("code", None)  # permanent
            enabled = data.pop("is_active", None)
            if data:
                rule = services.update_rule(rule, actor=request.user, **data)
            if enabled is not None:
                rule = services.set_rule_enabled(rule, enabled, actor=request.user)
        except DjangoValidationError as exc:
            _raise(exc)
        rule = ChecklistRule.objects.select_related("version").get(pk=rule.pk)
        return Response(ChecklistRuleSerializer(rule, context={"snapshot": snapshot_for_version(draft)}).data)

    @extend_schema(request=RuleValidateSerializer, responses=dict)
    @action(detail=True, methods=["post"], url_path="rules/validate")
    def rules_validate(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        """Validate the version's rules, or a candidate rule list, without saving anything (needs edit rights)."""
        template = self.get_object()
        if not policies.can_update_template(request.user, template):
            raise PermissionDenied
        payload = RuleValidateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        version = self._rules_version(template, request)
        snapshot = snapshot_for_version(version)
        candidates = payload.validated_data.get("rules")
        if candidates is not None:
            snapshot = snapshot.with_rules(
                [RuleSpec(str(r.get("code", f"CANDIDATE-{n}")), str(r.get("name", "")), r.get("condition"), r.get("actions"),
                          bool(r.get("is_active", True)), n, str(r.get("description", "")))
                 for n, r in enumerate(candidates, start=1) if isinstance(r, dict)]
            )
        return Response(validate_rule_set(snapshot).as_dict())

    @extend_schema(request=SimulationSerializer, responses=dict)
    @action(detail=True, methods=["post"], url_path="rules/simulate")
    def rules_simulate(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        """Simulate: evaluate the rules for sample answers.  Persists nothing; uses the same evaluator as everything else."""
        from apps.checklists.rules import limit

        template = self.get_object()
        if int(request.META.get("CONTENT_LENGTH") or 0) > limit("max_simulation_payload_bytes"):
            return Response({"detail": "The simulation request is too large."}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        payload = SimulationSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        version = self._rules_version(template, request)
        snapshot = snapshot_for_version(version)
        if data.get("rules") is not None:  # trying unsaved rules is an authoring feature
            if not policies.can_update_template(request.user, template):
                raise PermissionDenied
            snapshot = snapshot.with_rules(
                [RuleSpec(str(r.get("code", f"CANDIDATE-{n}")), str(r.get("name", "")), r.get("condition"), r.get("actions"),
                          bool(r.get("is_active", True)), n) for n, r in enumerate(data["rules"], start=1) if isinstance(r, dict)]
            )
        result = evaluate(compile_rules(snapshot), data.get("answers", {}), data.get("not_applicable", []))
        return Response({"simulation": True, "version": version.version_number, **result.as_dict()})

    @extend_schema(responses=dict)
    @action(detail=True, methods=["get"], url_path="rules/compiled")
    def rules_compiled(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        """Everything a client needs to evaluate the rules locally (the documented algorithm): normalized rules plus
        each question's type, configuration, options and section.  The server stays the source of truth."""
        template = self.get_object()
        version = self._rules_version(template, request)
        snapshot = snapshot_for_version(version)
        compiled = compile_rules(snapshot)
        return Response({
            "version": version.version_number, "status": version.status,
            "rules": [{"code": r.code, "name": r.name, "enabled": r.is_active, "order": r.sort_order,
                       "condition": r.stored_condition, "actions": r.stored_actions} for r in compiled.rules],
            "valid": not any(i.level == "error" for i in compiled.issues),
            "items": {i.code: {"section": i.section_code, "answer_type": i.answer_type, "config": i.config, "required": i.required,
                               "active": i.active, "options": [{"value": o.value, "label": o.label, "active": o.is_active} for o in i.options]}
                      for i in snapshot.items.values()},
            "sections": {s.code: {"name": s.name} for s in snapshot.sections.values()},
        })


class RuleMetadataView(APIView):
    """``GET /api/v1/checklists/rules/metadata/`` - operators (and which answer types allow them), actions and limits."""

    permission_classes = (IsAuthenticated, IsActiveUser)

    @extend_schema(responses=dict)
    def get(self, request: Request) -> Response:
        return Response({**describe_operators(), "actions": describe_actions(), "limits": all_limits()})


class AnswerTypeListView(APIView):
    """``GET /api/v1/checklists/answer-types/`` - the answer types and their capabilities.

    Platform-level metadata (no tenant data), so any authenticated user may read it; the PWA caches it to
    validate locally.
    """

    permission_classes = (IsAuthenticated, IsActiveUser)

    @extend_schema(responses=AnswerTypeSerializer(many=True))
    def get(self, request: Request) -> Response:
        return Response(registry.describe())


class AnswerTypeDetailView(APIView):
    """``GET /api/v1/checklists/answer-types/{code}/`` - one type with its configuration schema and defaults."""

    permission_classes = (IsAuthenticated, IsActiveUser)

    @extend_schema(responses=AnswerTypeDetailSerializer)
    def get(self, request: Request, code: str) -> Response:
        code = code.lower()
        if code not in registry:
            raise NotFound("Unknown answer type.")
        return Response(registry.describe(code, detail=True))
