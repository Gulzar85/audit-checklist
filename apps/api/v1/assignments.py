"""``/api/v1/assignments/`` - checklist assignments.

A thin adapter like every v1 resource: the queryset is ``selectors.assignments_visible_to`` (organization isolation + location
scope, so an invisible assignment is a 404), authorization is ``apps.assignments.policies`` (403) and every write goes through
``apps.assignments.services``.  There is no DELETE and no way to write ``status``: the lifecycle is only reachable through the
explicit ``issue`` / ``cancel`` / ``reopen`` actions, and PATCH accepts safe fields only.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response

from apps.api.exceptions import ConflictAPIException, StructuredValidationError
from apps.api.permissions import IsActiveUser, PolicyPermission
from apps.assignments import policies, selectors, services
from apps.assignments.constants import AssignmentPriority
from apps.assignments.filters import AssignmentFilter
from apps.assignments.models import ChecklistAssignment
from apps.checklists import selectors as checklist_selectors
from apps.checklists.models import ChecklistVersion
from apps.core.utils import parse_uuid
from apps.locations.models import Location
from apps.permissions.registry import P

PROTECTED = ("organization", "created_by", "status", "number", "version", "location", "issued_by", "issued_at", "cancelled_by", "cancelled_at", "cancel_reason")


def _raise(exc: DjangoValidationError) -> None:
    code = getattr(exc, "assignment_code", "assignment_error")
    if isinstance(exc, services.AssignmentConflict):
        raise ConflictAPIException(detail={"detail": " ".join(exc.messages), "code": code}) from exc
    raise StructuredValidationError({"__all__": list(exc.messages)}, extra={"code": code}) from exc


class AssignmentSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    reference = serializers.CharField(read_only=True)
    number = serializers.IntegerField(read_only=True)
    organization = serializers.UUIDField(source="organization_id", read_only=True)
    status = serializers.CharField(read_only=True)
    effective_status = serializers.CharField(read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    priority = serializers.CharField(read_only=True)
    instructions = serializers.CharField(read_only=True)
    due_at = serializers.DateTimeField(read_only=True)
    checklist = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    assigned_to = serializers.SerializerMethodField()
    created_by = serializers.UUIDField(source="created_by_id", read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    issued_at = serializers.DateTimeField(read_only=True)
    cancelled_at = serializers.DateTimeField(read_only=True)
    cancel_reason = serializers.CharField(read_only=True)

    def get_checklist(self, obj: ChecklistAssignment) -> dict:
        v = obj.version
        return {"template": str(v.template_id), "name": v.template.name, "code": v.template.code, "version": v.version_number, "version_id": str(v.pk)}

    def get_location(self, obj: ChecklistAssignment) -> dict:
        cache = self.context.setdefault("_paths", {})
        if obj.organization_id not in cache:
            cache[obj.organization_id] = selectors.location_paths({obj.organization_id})
        return {"id": str(obj.location_id), "name": obj.location.name, "path": cache[obj.organization_id].get(obj.location_id, obj.location.name)}

    def get_assigned_to(self, obj: ChecklistAssignment) -> dict | None:
        u = obj.assigned_to
        return {"id": str(u.pk), "email": u.email, "name": u.display_name} if u else None


class AssignmentCreateSerializer(serializers.Serializer):
    version = serializers.UUIDField()
    location = serializers.UUIDField()
    priority = serializers.ChoiceField(choices=AssignmentPriority.choices, default=AssignmentPriority.NORMAL)
    due_at = serializers.DateTimeField(required=False, allow_null=True)
    instructions = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    assigned_to = serializers.UUIDField(required=False, allow_null=True)
    issue = serializers.BooleanField(required=False, default=False)


class AssignmentPatchSerializer(serializers.Serializer):
    """Safe fields only.  Anything else - ``status``, ``organization``, ``version`` ... - is rejected, never silently ignored."""

    priority = serializers.ChoiceField(choices=AssignmentPriority.choices, required=False)
    due_at = serializers.DateTimeField(required=False, allow_null=True)
    instructions = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    assigned_to = serializers.UUIDField(required=False, allow_null=True)

    def to_internal_value(self, data):  # noqa: ANN001, ANN201
        unknown = {k: "This field cannot be changed." for k in data if k not in self.fields}
        if unknown:
            raise serializers.ValidationError(unknown)
        return super().to_internal_value(data)


class CancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=300)


class AssigneeSerializer(serializers.Serializer):
    assigned_to = serializers.UUIDField(allow_null=True)


class DueSerializer(serializers.Serializer):
    due_at = serializers.DateTimeField(allow_null=True)


class PrioritySerializer(serializers.Serializer):
    priority = serializers.ChoiceField(choices=AssignmentPriority.choices)


class BulkSerializer(serializers.Serializer):
    version = serializers.UUIDField()
    locations = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=services.bulk.MAX_TARGETS)
    include_descendants = serializers.BooleanField(required=False, default=False)
    priority = serializers.ChoiceField(choices=AssignmentPriority.choices, default=AssignmentPriority.NORMAL)
    due_at = serializers.DateTimeField(required=False, allow_null=True)
    instructions = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    assigned_to = serializers.UUIDField(required=False, allow_null=True)
    issue = serializers.BooleanField(required=False, default=False)
    dry_run = serializers.BooleanField(required=False, default=False)


class AssignmentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    serializer_class = AssignmentSerializer
    permission_classes = [IsActiveUser, PolicyPermission]
    filterset_class = AssignmentFilter
    search_fields = ("version__template__name", "version__template__code", "location__name", "location__code", "instructions")
    ordering_fields = ("number", "due_at", "created_at", "issued_at", "status", "priority_rank")
    ordering = ("-created_at",)
    http_method_names = ["get", "post", "patch", "head", "options"]  # no PUT, no DELETE

    def get_queryset(self):  # noqa: ANN201
        return selectors.assignments_visible_to(self.request.user)

    def policy_allows_collection(self, request: Request) -> bool:
        return True  # the concrete location is authorized by the service (create) or the object hook

    def policy_allows_object(self, request: Request, obj: ChecklistAssignment) -> bool:
        # Lifecycle actions: scope/permission here, then the service reports an invalid transition or a repeat (409/400).
        if self.action == "issue":
            return policies.can_issue_at(request.user, obj.location)
        if self.action == "cancel":
            return policies.can_cancel_at(request.user, obj.location)
        if self.action == "reopen":
            return policies.can_reopen_at(request.user, obj.location)
        if self.action in ("change_assignee", "change_due_date", "change_priority"):
            return policies.can_change_details(request.user, obj)
        if self.action == "partial_update":
            return policies.can_edit_draft(request.user, obj) or policies.can_change_details(request.user, obj)
        return True  # read

    # -- helpers ------------------------------------------------------------------------------
    def _out(self, assignment: ChecklistAssignment) -> Response:
        fresh = self.get_queryset().get(pk=assignment.pk)
        return Response(self.get_serializer(fresh).data)

    def _version(self, raw) -> ChecklistVersion:  # noqa: ANN001
        templates = checklist_selectors.templates_for_user(self.request.user)
        found = ChecklistVersion.objects.filter(pk=raw, template__in=templates.values("pk")).select_related("template", "template__organization").first()
        if found is None:  # not visible == not found; never confirm that it exists elsewhere
            raise serializers.ValidationError({"version": "Unknown checklist version."})
        return found

    def _location(self, raw) -> Location:  # noqa: ANN001
        found = Location.objects.filter(pk=parse_uuid(raw)).select_related("organization").first() if parse_uuid(raw) else None
        if found is None or not (policies.can_create_assignment(self.request.user, found) or policies.can_bulk_create(self.request.user, found) or policies._at(self.request.user, P.ASSIGNMENT_VIEW, found)):
            raise serializers.ValidationError({"location": "Unknown location."})
        return found

    def _user(self, raw):  # noqa: ANN001, ANN202
        from django.contrib.auth import get_user_model

        if raw is None:
            return None
        found = get_user_model().objects.filter(pk=raw).first()
        if found is None:
            raise serializers.ValidationError({"assigned_to": "Unknown user."})
        return found

    def _run(self, call):  # noqa: ANN001, ANN202
        try:
            return call()
        except DjangoPermissionDenied as exc:
            raise PermissionDenied from exc
        except DjangoValidationError as exc:
            _raise(exc)

    # -- writes -------------------------------------------------------------------------------
    @extend_schema(request=AssignmentCreateSerializer, responses=AssignmentSerializer)
    def create(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        payload = AssignmentCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        location = self._location(d["location"])
        if not policies.can_create_assignment(request.user, location):
            raise PermissionDenied  # known to the caller but not theirs to assign to
        version = self._version(d["version"])
        assignee = self._user(d.get("assigned_to"))
        created = self._run(lambda: services.create_assignment(
            actor=request.user, version=version, location=location, priority=d["priority"], due_at=d.get("due_at"),
            instructions=d.get("instructions", ""), assigned_to=assignee, issue=d["issue"]))
        return Response(self._out(created).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=AssignmentPatchSerializer, responses=AssignmentSerializer)
    def partial_update(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        assignment = self.get_object()
        payload = AssignmentPatchSerializer(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        if "assigned_to" in d:
            d["assigned_to"] = self._user(d["assigned_to"])
        if assignment.status == "draft":
            self._run(lambda: services.update_draft(assignment, actor=request.user, **d))
        else:  # issued: only the safe, individually audited changes
            if "instructions" in d:
                raise serializers.ValidationError({"instructions": "Instructions can only be edited on a draft."})
            if "priority" in d:
                self._run(lambda: services.change_priority(assignment, actor=request.user, priority=d["priority"]))
            if "due_at" in d:
                self._run(lambda: services.change_due_date(assignment, actor=request.user, due_at=d["due_at"]))
            if "assigned_to" in d:
                self._run(lambda: services.change_assignee(assignment, actor=request.user, assigned_to=d["assigned_to"]))
        return self._out(assignment)

    def update(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        if not kwargs.get("partial"):
            from rest_framework.exceptions import MethodNotAllowed

            raise MethodNotAllowed("PUT")
        return self.partial_update(request, *args, **kwargs)

    @extend_schema(request=None, responses=AssignmentSerializer)
    @action(detail=True, methods=["post"])
    def issue(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        assignment = self.get_object()
        self._run(lambda: services.issue(assignment, actor=request.user))
        return self._out(assignment)

    @extend_schema(request=CancelSerializer, responses=AssignmentSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        assignment = self.get_object()
        payload = CancelSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        self._run(lambda: services.cancel(assignment, actor=request.user, reason=payload.validated_data["reason"]))
        return self._out(assignment)

    @extend_schema(request=None, responses=AssignmentSerializer)
    @action(detail=True, methods=["post"])
    def reopen(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        assignment = self.get_object()
        self._run(lambda: services.reopen(assignment, actor=request.user))
        return self._out(assignment)

    @extend_schema(request=AssigneeSerializer, responses=AssignmentSerializer)
    @action(detail=True, methods=["post"], url_path="change-assignee")
    def change_assignee(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        assignment = self.get_object()
        payload = AssigneeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        user = self._user(payload.validated_data["assigned_to"])
        self._run(lambda: services.change_assignee(assignment, actor=request.user, assigned_to=user))
        return self._out(assignment)

    @extend_schema(request=DueSerializer, responses=AssignmentSerializer)
    @action(detail=True, methods=["post"], url_path="change-due-date")
    def change_due_date(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        assignment = self.get_object()
        payload = DueSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        self._run(lambda: services.change_due_date(assignment, actor=request.user, due_at=payload.validated_data["due_at"]))
        return self._out(assignment)

    @extend_schema(request=PrioritySerializer, responses=AssignmentSerializer)
    @action(detail=True, methods=["post"], url_path="change-priority")
    def change_priority(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        assignment = self.get_object()
        payload = PrioritySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        self._run(lambda: services.change_priority(assignment, actor=request.user, priority=payload.validated_data["priority"]))
        return self._out(assignment)

    @extend_schema(request=BulkSerializer)
    @action(detail=False, methods=["post"], url_path="bulk-create")
    def bulk_create(self, request: Request) -> Response:
        payload = BulkSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        version = self._version(d["version"])
        ids = [str(i) for i in d["locations"]]
        if d["include_descendants"]:
            roots = list(Location.objects.filter(pk__in=ids))
            targets, _skipped = selectors.expand_targets(request.user, roots, include_descendants=True)
            ids = list(dict.fromkeys([*ids, *(str(t.pk) for t in targets)]))
        assignee = self._user(d.get("assigned_to"))
        result = self._run(lambda: services.bulk_create_assignments(
            actor=request.user, version=version, location_ids=ids, priority=d["priority"], due_at=d.get("due_at"),
            instructions=d.get("instructions", ""), assigned_to=assignee, issue=d["issue"], dry_run=d["dry_run"]))
        report = result.as_dict()
        report["targets"] = report.pop("results")  # (a dict with "count" + "results" would be read as a paginated list)
        if d["dry_run"]:
            return Response(report)  # a report, never a write
        if not result.created:  # all-or-nothing: nothing was created; say, per location, why
            raise StructuredValidationError({"__all__": ["Nothing was created: at least one location cannot be assigned."]}, extra=report)
        return Response(report, status=status.HTTP_201_CREATED)

    @extend_schema(responses=None)
    @action(detail=False, methods=["get"])
    def summary(self, request: Request) -> Response:
        queryset = self.filter_queryset(self.get_queryset())
        return Response(selectors.summary(queryset))

    @extend_schema(responses=None)
    @action(detail=True, methods=["get"])
    def history(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        assignment = self.get_object()
        rows = [{"verb": e.verb, "description": e.description, "created_at": e.created_at, "actor": e.actor.email if e.actor else None,
                 "metadata": e.metadata} for e in selectors.timeline(assignment)]
        return Response({"results": rows})




