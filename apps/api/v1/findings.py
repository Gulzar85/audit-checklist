"""``/api/v1/findings/`` (+ ``finding-categories`` and ``finding-severities``).

A thin adapter like every v1 resource: the queryset is ``selectors.findings_visible_to`` (organization isolation + location scope, so
an invisible finding is a 404), authorization is ``apps.findings.policies`` (403) and every write goes through ``apps.findings.services``.
There is no DELETE and no way to write ``status``: the lifecycle is reachable only through the explicit action endpoints, PATCH accepts
triage fields only, and a client can never supply the organization of a finding, its creator, its source, its detection data or its
status (unknown fields are rejected, not ignored).  There is no endpoint that triggers rule evaluation: findings are raised by the
server when an execution is submitted.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response

from apps.api.exceptions import ConflictAPIException, StructuredValidationError
from apps.api.permissions import IsActiveUser, PolicyPermission
from apps.core.utils import parse_uuid
from apps.findings import policies, selectors, services
from apps.findings.constants import TRANSITIONS
from apps.findings.files import evidence_response
from apps.findings.filters import FindingFilter
from apps.findings.models import Finding, FindingCategory, FindingSeverity
from apps.locations.models import Location
from apps.permissions.authorization import has_permission_anywhere, visible_organization_ids
from apps.permissions.registry import P


def _raise(exc: DjangoValidationError) -> None:
    code = getattr(exc, "finding_code", "finding_error")
    if isinstance(exc, services.FindingConflict):
        raise ConflictAPIException(detail={"detail": " ".join(exc.messages), "code": code}) from exc
    raise StructuredValidationError({"__all__": list(exc.messages)}, extra={"code": code}) from exc


class _Strict(serializers.Serializer):
    """Reject unknown fields instead of silently ignoring them (``organization``, ``status``, ``detected_by``, ``source_type`` ...)."""

    def to_internal_value(self, data):  # noqa: ANN001, ANN201
        unknown = {k: "This field cannot be set." for k in data if k not in self.fields}
        if unknown:
            raise serializers.ValidationError(unknown)
        return super().to_internal_value(data)


# -- output ---------------------------------------------------------------------------------------------------------------
def _brief(obj, *fields):  # noqa: ANN001, ANN202
    return {f: (str(getattr(obj, f)) if f == "id" else getattr(obj, f)) for f in fields} if obj is not None else None


class FindingSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    reference = serializers.CharField(read_only=True)
    number = serializers.IntegerField(read_only=True)
    organization = serializers.UUIDField(source="organization_id", read_only=True)
    title = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    effective_status = serializers.CharField(read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    source_type = serializers.CharField(read_only=True)
    needs_review = serializers.BooleanField(read_only=True)
    rule_code = serializers.CharField(read_only=True)
    rule_name = serializers.CharField(read_only=True)
    detected_at = serializers.DateTimeField(read_only=True)
    detected_by = serializers.UUIDField(source="detected_by_id", read_only=True)
    due_at = serializers.DateTimeField(read_only=True)
    resolution_summary = serializers.CharField(read_only=True)
    resolved_at = serializers.DateTimeField(read_only=True)
    closed_at = serializers.DateTimeField(read_only=True)
    cancelled_at = serializers.DateTimeField(read_only=True)
    cancel_reason = serializers.CharField(read_only=True)
    reopen_count = serializers.IntegerField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    category = serializers.SerializerMethodField()
    severity = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    assigned_to = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()

    def get_category(self, obj: Finding) -> dict:
        return _brief(obj.category, "id", "code", "name")

    def get_severity(self, obj: Finding) -> dict:
        return _brief(obj.severity, "id", "code", "name", "rank", "is_escalation")

    def get_location(self, obj: Finding) -> dict:
        return {"id": str(obj.location_id), "name": obj.location.name, "path": obj.location.display_path}

    def get_assigned_to(self, obj: Finding) -> dict | None:
        u = obj.assigned_to
        return {"id": str(u.pk), "email": u.email, "name": u.display_name} if u else None

    def get_source(self, obj: Finding) -> dict | None:
        if obj.execution_id is None:
            return None
        return {"assignment": str(obj.assignment_id), "execution": str(obj.execution_id), "execution_reference": obj.execution.reference,
                "checklist_version": str(obj.version_id), "checklist": obj.version.template.name, "version_number": obj.version.version_number,
                "item": str(obj.item_id) if obj.item_id else None, "question": obj.item.question if obj.item_id else None,
                "response": str(obj.response_id) if obj.response_id else None}

    def to_representation(self, instance):  # noqa: ANN001, ANN201
        data = super().to_representation(instance)
        if self.context.get("detail"):
            request = self.context["request"]
            data["allowed_actions"] = [t.action for t in policies.available_actions(request.user, instance)]
            data["can_update"], data["can_assign"] = policies.can_update(request.user, instance), policies.can_assign(request.user, instance)
        return data


class CategorySerializer(serializers.ModelSerializer):
    organization = serializers.UUIDField(source="organization_id", read_only=True)

    class Meta:
        model = FindingCategory
        fields = ("id", "organization", "code", "name", "description", "is_active", "is_system", "sort_order")
        read_only_fields = fields


class SeveritySerializer(serializers.ModelSerializer):
    organization = serializers.UUIDField(source="organization_id", read_only=True)

    class Meta:
        model = FindingSeverity
        fields = ("id", "organization", "code", "name", "rank", "is_default", "is_active", "requires_owner", "default_due_days", "is_escalation", "is_system")
        read_only_fields = fields


# -- input ----------------------------------------------------------------------------------------------------------------
class FindingCreateSerializer(_Strict):
    location = serializers.UUIDField()
    title = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    category = serializers.UUIDField()
    severity = serializers.UUIDField()
    execution = serializers.UUIDField(required=False, allow_null=True)
    item = serializers.UUIDField(required=False, allow_null=True)
    assigned_to = serializers.UUIDField(required=False, allow_null=True)
    due_at = serializers.DateTimeField(required=False, allow_null=True)


class FindingPatchSerializer(_Strict):
    title = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    category = serializers.UUIDField(required=False)
    severity = serializers.UUIDField(required=False)
    due_at = serializers.DateTimeField(required=False, allow_null=True)


class ReasonSerializer(_Strict):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class AssignSerializer(_Strict):
    assigned_to = serializers.UUIDField(allow_null=True)
    due_at = serializers.DateTimeField(required=False, allow_null=True)


class CategoryWriteSerializer(_Strict):
    organization = serializers.UUIDField(required=False)
    code = serializers.CharField(max_length=40, required=False)
    name = serializers.CharField(max_length=100, required=False)
    description = serializers.CharField(max_length=300, required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)
    sort_order = serializers.IntegerField(required=False, min_value=0)


class SeverityWriteSerializer(_Strict):
    organization = serializers.UUIDField(required=False)
    code = serializers.CharField(max_length=40, required=False)
    name = serializers.CharField(max_length=100, required=False)
    rank = serializers.IntegerField(required=False)
    requires_owner = serializers.BooleanField(required=False)
    default_due_days = serializers.IntegerField(required=False, allow_null=True)
    is_escalation = serializers.BooleanField(required=False)
    is_default = serializers.BooleanField(required=False)
    is_active = serializers.BooleanField(required=False)


class FindingViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    serializer_class = FindingSerializer
    permission_classes = [IsActiveUser, PolicyPermission]
    filterset_class = FindingFilter
    search_fields = ("title", "description", "location__name")
    ordering_fields = ("number", "detected_at", "due_at", "status", "severity__rank", "created_at")
    ordering = ("-detected_at",)
    http_method_names = ["get", "post", "patch", "head", "options"]  # no PUT, no DELETE

    def get_queryset(self):  # noqa: ANN201
        return selectors.findings_visible_to(self.request.user)

    def get_serializer_context(self):  # noqa: ANN201
        return {**super().get_serializer_context(), "detail": self.action != "list"}

    def policy_allows_collection(self, request: Request) -> bool:
        if self.action == "create":
            return has_permission_anywhere(request.user, P.FINDING_CREATE)
        return True

    def policy_allows_object(self, request: Request, obj: Finding) -> bool:
        if self.action in TRANSITIONS:
            return policies.can_act(request.user, obj, self.action)
        if self.action == "partial_update":
            return policies._at(request.user, P.FINDING_UPDATE, obj.location)
        if self.action == "assign":
            return policies._at(request.user, P.FINDING_ASSIGN, obj.location)
        return True

    # -- helpers ------------------------------------------------------------------------------
    def _out(self, finding: Finding, code: int = 200) -> Response:
        fresh = self.get_queryset().get(pk=finding.pk)
        return Response(self.get_serializer(fresh).data, status=code)

    def _run(self, call):  # noqa: ANN001, ANN202
        try:
            return call()
        except DjangoPermissionDenied as exc:
            raise PermissionDenied from exc
        except DjangoValidationError as exc:
            _raise(exc)

    def _location(self, raw) -> Location:  # noqa: ANN001
        found = Location.objects.filter(pk=raw).select_related("organization").first()
        if found is None or not (policies.can_create_at(self.request.user, found) or policies._at(self.request.user, P.FINDING_VIEW, found)):
            raise serializers.ValidationError({"location": "Unknown location."})
        return found

    def _classification(self, model, raw, field):  # noqa: ANN001, ANN202
        found = (selectors.visible_categories if model is FindingCategory else selectors.visible_severities)(self.request.user).filter(pk=raw).first()
        if found is None:
            raise serializers.ValidationError({field: f"Unknown {field}."})
        return found

    def _user(self, raw):  # noqa: ANN001, ANN202
        from django.contrib.auth import get_user_model

        if raw is None:
            return None
        found = get_user_model().objects.filter(pk=raw).first()
        if found is None:
            raise serializers.ValidationError({"assigned_to": "Unknown user."})
        return found

    # -- writes -------------------------------------------------------------------------------
    @extend_schema(request=FindingCreateSerializer, responses=FindingSerializer)
    def create(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        payload = FindingCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        location = self._location(d["location"])
        if not policies.can_create_at(request.user, location):
            raise PermissionDenied
        execution = item = None
        if d.get("execution"):
            from apps.executions import selectors as exec_selectors

            execution = exec_selectors.get_execution(request.user, d["execution"])
            if execution is None:
                raise serializers.ValidationError({"execution": "Unknown checklist execution."})
        if d.get("item"):
            from apps.checklists.models import ChecklistItem

            item = ChecklistItem.objects.filter(pk=d["item"]).first()
            if item is None:
                raise serializers.ValidationError({"item": "Unknown question."})
        created = self._run(lambda: services.create_manual(
            actor=request.user, location=location, title=d["title"], description=d.get("description", ""),
            category=self._classification(FindingCategory, d["category"], "category"), severity=self._classification(FindingSeverity, d["severity"], "severity"),
            execution=execution, item=item, assigned_to=self._user(d.get("assigned_to")), due_at=d.get("due_at")))
        return self._out(created, status.HTTP_201_CREATED)

    @extend_schema(request=FindingPatchSerializer, responses=FindingSerializer)
    def partial_update(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        finding = self.get_object()
        payload = FindingPatchSerializer(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        d = dict(payload.validated_data)
        if "category" in d:
            d["category"] = self._classification(FindingCategory, d["category"], "category")
        if "severity" in d:
            d["severity"] = self._classification(FindingSeverity, d["severity"], "severity")
        self._run(lambda: services.update_finding(finding, actor=request.user, **d))
        return self._out(finding)

    def update(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        if not kwargs.get("partial"):
            from rest_framework.exceptions import MethodNotAllowed

            raise MethodNotAllowed("PUT")
        return self.partial_update(request, *args, **kwargs)

    def _transition(self, request: Request, name: str) -> Response:
        finding = self.get_object()
        payload = ReasonSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        self._run(lambda: services.transition(finding, name, actor=request.user, reason=payload.validated_data.get("reason", "")))
        return self._out(finding)

    @extend_schema(request=ReasonSerializer, responses=FindingSerializer)
    @action(detail=True, methods=["post"])
    def acknowledge(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request, "acknowledge")

    @extend_schema(request=ReasonSerializer, responses=FindingSerializer)
    @action(detail=True, methods=["post"])
    def start(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request, "start")

    @extend_schema(request=ReasonSerializer, responses=FindingSerializer)
    @action(detail=True, methods=["post"])
    def resolve(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request, "resolve")

    @extend_schema(request=ReasonSerializer, responses=FindingSerializer)
    @action(detail=True, methods=["post"])
    def close(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request, "close")

    @extend_schema(request=ReasonSerializer, responses=FindingSerializer)
    @action(detail=True, methods=["post"])
    def reopen(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request, "reopen")

    @extend_schema(request=ReasonSerializer, responses=FindingSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request, "cancel")

    @extend_schema(request=AssignSerializer, responses=FindingSerializer)
    @action(detail=True, methods=["post"])
    def assign(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        finding = self.get_object()
        payload = AssignSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        extra = {"due_at": d["due_at"]} if "due_at" in d else {}
        self._run(lambda: services.assign(finding, actor=request.user, assigned_to=self._user(d["assigned_to"]), **extra))
        return self._out(finding)

    # -- reads --------------------------------------------------------------------------------
    @extend_schema(responses=None)
    @action(detail=False, methods=["get"])
    def summary(self, request: Request) -> Response:
        return Response(selectors.summary(self.filter_queryset(self.get_queryset())))

    @extend_schema(responses=None)
    @action(detail=True, methods=["get"])
    def history(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        finding = self.get_object()
        rows = [{"verb": e.verb, "description": e.description, "created_at": e.created_at, "actor": e.actor.email if e.actor else None,
                 "metadata": e.metadata} for e in selectors.timeline(finding)]
        return Response({"results": rows})

    @extend_schema(responses=None)
    @action(detail=True, methods=["get"])
    def evidence(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        from apps.evidence.selectors import is_servable

        finding = self.get_object()
        base = f"/api/v1/findings/{finding.pk}/evidence"
        rows = [{"id": str(a.pk), "name": a.asset.original_name, "kind": a.asset.kind, "content_type": a.asset.content_type, "size": a.asset.size,
                 "status": a.asset.status, "available": is_servable(a.asset), "caption": a.caption,
                 "download_url": f"{base}/{a.pk}/download/" if is_servable(a.asset) else None,
                 "preview_url": f"{base}/{a.pk}/preview/" if is_servable(a.asset) and a.asset.is_image else None}
                for a in selectors.evidence_for(finding)]
        return Response({"results": rows})

    def _file(self, request: Request, attachment: str, inline: bool):  # noqa: ANN202
        finding = self.get_object()
        response = evidence_response(request.user, finding, attachment, inline=inline)
        if response is None:
            raise Http404
        return response

    @extend_schema(responses=None)
    @action(detail=True, methods=["get"], url_path=r"evidence/(?P<attachment>[0-9a-f-]{36})/download")
    def evidence_download(self, request: Request, pk=None, attachment=None):  # noqa: ANN001, ANN201
        return self._file(request, attachment, False)

    @extend_schema(responses=None)
    @action(detail=True, methods=["get"], url_path=r"evidence/(?P<attachment>[0-9a-f-]{36})/preview")
    def evidence_preview(self, request: Request, pk=None, attachment=None):  # noqa: ANN001, ANN201
        return self._file(request, attachment, True)


# -- configuration --------------------------------------------------------------------------------------------------------
class _ConfigViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """Categories / severities: read for anyone who may see findings; writes need ``finding.manage`` in the named organization."""

    permission_classes = [IsActiveUser, PolicyPermission]
    http_method_names = ["get", "post", "patch", "head", "options"]
    write_serializer: type[serializers.Serializer]
    ordering = ()

    def policy_allows_collection(self, request: Request) -> bool:
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return has_permission_anywhere(request.user, P.FINDING_VIEW) or has_permission_anywhere(request.user, P.FINDING_MANAGE)
        return has_permission_anywhere(request.user, P.FINDING_MANAGE)

    def policy_allows_object(self, request: Request, obj) -> bool:  # noqa: ANN001
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        from apps.organizations.models import Organization

        return policies.can_manage_config(request.user, Organization.objects.get(pk=obj.organization_id))

    def _organization(self, raw):  # noqa: ANN001, ANN202
        from apps.organizations.models import Organization

        ids = [str(i) for i in visible_organization_ids(self.request.user)]
        if raw is None and len(ids) == 1:
            raw = ids[0]
        organization = Organization.objects.filter(pk=raw).first() if raw and str(raw) in ids else None
        if organization is None:
            raise serializers.ValidationError({"organization": "Name one of your organizations."})
        return organization

    def _run(self, call):  # noqa: ANN001, ANN202
        try:
            return call()
        except DjangoPermissionDenied as exc:
            raise PermissionDenied from exc
        except DjangoValidationError as exc:
            _raise(exc)

    def _save(self, request: Request, instance):  # noqa: ANN001, ANN202 - pragma: no cover - interface
        raise NotImplementedError

    def create(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        saved = self._save(request, None)
        return Response(self.get_serializer(saved).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        saved = self._save(request, self.get_object())
        return Response(self.get_serializer(saved).data)

    def update(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        if not kwargs.get("partial"):
            from rest_framework.exceptions import MethodNotAllowed

            raise MethodNotAllowed("PUT")
        return self.partial_update(request, *args, **kwargs)


class CategoryViewSet(_ConfigViewSet):
    serializer_class = CategorySerializer
    filterset_fields = ("is_active",)

    def get_queryset(self):  # noqa: ANN201
        return selectors.visible_categories(self.request.user).order_by("sort_order", "name")

    def _save(self, request: Request, instance):  # noqa: ANN001, ANN202
        payload = CategoryWriteSerializer(data=request.data, partial=instance is not None)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        organization = self._organization(d.get("organization")) if instance is None else instance.organization
        if instance is not None and "organization" in d:
            raise serializers.ValidationError({"organization": "The organization of a category never changes."})
        kwargs = {k: d[k] for k in ("code", "name", "description", "is_active", "sort_order") if k in d}
        if instance is not None:
            kwargs = {"name": instance.name, "description": instance.description, "is_active": instance.is_active, **kwargs}
        return self._run(lambda: services.save_category(actor=request.user, organization=organization, category=instance, **kwargs))


class SeverityViewSet(_ConfigViewSet):
    serializer_class = SeveritySerializer
    filterset_fields = ("is_active",)

    def get_queryset(self):  # noqa: ANN201
        return selectors.visible_severities(self.request.user).order_by("-rank")

    def _save(self, request: Request, instance):  # noqa: ANN001, ANN202
        payload = SeverityWriteSerializer(data=request.data, partial=instance is not None)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        organization = self._organization(d.get("organization")) if instance is None else instance.organization
        if instance is not None and "organization" in d:
            raise serializers.ValidationError({"organization": "The organization of a severity level never changes."})
        kwargs = {k: d[k] for k in ("code", "name", "rank", "requires_owner", "default_due_days", "is_escalation", "is_default", "is_active") if k in d}
        if instance is not None:
            current = {"name": instance.name, "rank": instance.rank, "requires_owner": instance.requires_owner, "default_due_days": instance.default_due_days,
                       "is_escalation": instance.is_escalation, "is_default": instance.is_default, "is_active": instance.is_active}
            kwargs = {**current, **kwargs}
        return self._run(lambda: services.save_severity(actor=request.user, organization=organization, severity=instance, **kwargs))


_ = parse_uuid
