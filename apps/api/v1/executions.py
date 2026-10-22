"""``/api/v1/executions/`` and the assignment-side work endpoints.

A thin adapter like every v1 resource: querysets are selectors (an invisible execution is a 404), authorization is the execution
policies applied by the services (a visible but forbidden operation is a 403), and every write is a service call - the same ones the
HTML workspace uses, so answers are validated, rules evaluated, progress computed and submission performed exactly once, in one place.
There is no DELETE, and no way to write ``status``, ``organization``, ``assignment``, ``version`` or the submitter: protected fields
are rejected with a 400, never silently ignored.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import generics, mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

import django_filters

from apps.api.exceptions import ConflictAPIException, StructuredValidationError
from apps.api.permissions import IsActiveUser
from apps.api.v1.assignments import AssignmentSerializer
from apps.assignments import selectors as assignment_selectors
from apps.executions import policies, selectors, services
from apps.executions.constants import ExecutionStatus
from apps.executions.models import ChecklistExecution, ChecklistResponse
from apps.executions.services.responses import AnswerInput

PROTECTED = ("organization", "assignment", "version", "status", "started_by", "submitted_by", "submitted_at", "number", "item", "execution")


def _raise(exc: DjangoValidationError) -> None:
    code = getattr(exc, "execution_code", "execution_error")
    if isinstance(exc, services.ExecutionConflict):
        raise ConflictAPIException(detail={"detail": " ".join(exc.messages), "code": code}) from exc
    if isinstance(exc, services.ExecutionValidationError):
        raise StructuredValidationError({"__all__": [str(exc.message)]}, extra={"code": code, "problems": exc.problems}) from exc
    raise StructuredValidationError({"__all__": list(exc.messages)}, extra={"code": code}) from exc


def run(call):  # noqa: ANN001, ANN201
    try:
        return call()
    except DjangoPermissionDenied as exc:
        raise PermissionDenied from exc
    except DjangoValidationError as exc:
        _raise(exc)


class ExecutionSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    reference = serializers.CharField(read_only=True)
    number = serializers.IntegerField(read_only=True)
    organization = serializers.UUIDField(source="organization_id", read_only=True)
    status = serializers.CharField(read_only=True)
    revision = serializers.IntegerField(read_only=True)
    assignment = serializers.SerializerMethodField()
    checklist = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    started_by = serializers.UUIDField(source="started_by_id", read_only=True)
    started_at = serializers.DateTimeField(read_only=True)
    last_activity_at = serializers.DateTimeField(read_only=True)
    submitted_by = serializers.UUIDField(source="submitted_by_id", read_only=True)
    submitted_at = serializers.DateTimeField(read_only=True)
    cancelled_at = serializers.DateTimeField(read_only=True)
    cancel_reason = serializers.CharField(read_only=True)

    def get_assignment(self, obj: ChecklistExecution) -> dict:
        a = obj.assignment
        return {"id": str(a.pk), "reference": a.reference, "status": a.status, "due_at": a.due_at}

    def get_checklist(self, obj: ChecklistExecution) -> dict:
        v = obj.version
        return {"template": str(v.template_id), "name": v.template.name, "code": v.template.code, "version": v.version_number, "version_id": str(v.pk)}

    def get_location(self, obj: ChecklistExecution) -> dict:
        loc = obj.assignment.location
        cache = self.context.setdefault("_paths", {})
        if obj.organization_id not in cache:
            cache[obj.organization_id] = assignment_selectors.location_paths({obj.organization_id})
        return {"id": str(loc.pk), "name": loc.name, "path": cache[obj.organization_id].get(loc.pk, loc.name)}


class ResponseSerializer(serializers.Serializer):
    item = serializers.UUIDField(source="item_id", read_only=True)
    code = serializers.CharField(source="item.code", read_only=True)
    question = serializers.CharField(source="item.question", read_only=True)
    answer_type = serializers.CharField(source="item.answer_type", read_only=True)
    status = serializers.CharField(read_only=True)
    value = serializers.JSONField(read_only=True)
    comment = serializers.CharField(read_only=True)
    revision = serializers.IntegerField(read_only=True)
    answered_at = serializers.DateTimeField(read_only=True)


class _Strict(serializers.Serializer):
    """Rejects unknown keys (notably the protected fields) instead of ignoring them."""

    def to_internal_value(self, data):  # noqa: ANN001, ANN201
        if isinstance(data, dict):
            unknown = {k: "This field cannot be set." if k in PROTECTED else "Unknown field." for k in data if k not in self.fields}
            if unknown:
                raise serializers.ValidationError(unknown)
        return super().to_internal_value(data)


class AnswerSerializer(_Strict):
    value = serializers.JSONField(required=False, allow_null=True)
    not_applicable = serializers.BooleanField(required=False, default=False)
    comment = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    revision = serializers.IntegerField(required=False, min_value=0, default=0)


class BatchItemSerializer(AnswerSerializer):
    item = serializers.UUIDField()


class BatchSerializer(_Strict):
    responses = BatchItemSerializer(many=True, allow_empty=False, max_length=500)


class CancelSerializer(_Strict):
    reason = serializers.CharField(max_length=300)


class MyAssignmentSerializer(AssignmentSerializer):
    execution = serializers.SerializerMethodField()

    def get_execution(self, obj) -> dict | None:  # noqa: ANN001
        live = getattr(obj, "live_executions", None)
        return {"id": str(live[0].pk), "status": live[0].status} if live else None


class ExecutionFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ExecutionStatus.choices)
    assignment = django_filters.UUIDFilter(field_name="assignment_id")
    template = django_filters.UUIDFilter(field_name="version__template_id")
    version = django_filters.NumberFilter(field_name="version__version_number")
    location = django_filters.UUIDFilter(field_name="assignment__location_id")
    started_after = django_filters.IsoDateTimeFilter(field_name="started_at", lookup_expr="gte")
    started_before = django_filters.IsoDateTimeFilter(field_name="started_at", lookup_expr="lte")

    class Meta:
        model = ChecklistExecution
        fields = ("status", "assignment", "template", "version", "location")


def _fresh(user, execution):  # noqa: ANN001, ANN202
    return selectors.executions_visible_to(user).get(pk=execution.pk)


class ExecutionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = ExecutionSerializer
    permission_classes = [IsActiveUser]
    filterset_class = ExecutionFilter
    ordering_fields = ("number", "started_at", "submitted_at", "status")
    ordering = ("-started_at",)
    http_method_names = ["get", "put", "post", "head", "options"]  # no PATCH of an execution, no DELETE

    def get_queryset(self):  # noqa: ANN201
        return selectors.executions_visible_to(self.request.user)

    def _out(self, execution: ChecklistExecution) -> Response:
        return Response(self.get_serializer(_fresh(self.request.user, execution)).data)

    # -- reads --------------------------------------------------------------------------------------------------
    @extend_schema(responses=None)
    @action(detail=True, methods=["get"])
    def progress(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return Response(services.compute_progress(services.build_state(self.get_object())))

    @extend_schema(responses=ResponseSerializer(many=True))
    @action(detail=True, methods=["get"])
    def responses(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        execution = self.get_object()
        rows = ChecklistResponse.objects.filter(execution=execution, is_hidden=False).select_related("item")
        return Response({"results": ResponseSerializer(rows, many=True).data, "revision": execution.revision})

    @extend_schema(responses=None)
    @action(detail=True, methods=["get"])
    def history(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        rows = [{"verb": e.verb, "description": e.description, "created_at": e.created_at, "actor": e.actor.email if e.actor else None,
                 "metadata": e.metadata} for e in selectors.timeline(self.get_object())]
        return Response({"results": rows})

    # -- writes -------------------------------------------------------------------------------------------------
    def _saved(self, request: Request, execution: ChecklistExecution, inputs: list[AnswerInput]) -> Response:
        result = run(lambda: services.save_answers(execution, actor=request.user, inputs=inputs))
        body = {
            "saved": result.saved, "cleared": result.cleared, "revision": result.execution.revision,
            "progress": services.compute_progress(result.state),
        }
        if result.errors:
            raise StructuredValidationError({"__all__": ["Some answers were not saved."]}, extra={"code": "validation_failed", "errors": result.errors, **body})
        if result.conflicts:
            conflict = ConflictAPIException(detail={"detail": "Some answers were changed since you loaded them.", "code": "stale_revision"})
            conflict.extra = {"conflicts": result.conflicts, **body}  # typed data: DRF would stringify it inside ``detail``
            raise conflict
        return Response(body)

    @extend_schema(request=AnswerSerializer, responses=None)
    @action(detail=True, methods=["put"], url_path=r"responses/(?P<item_id>[^/.]+)")
    def response(self, request: Request, pk=None, item_id=None) -> Response:  # noqa: ANN001
        execution = self.get_object()
        payload = AnswerSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        return self._saved(request, execution, [AnswerInput(str(item_id), d.get("value"), d["not_applicable"], d.get("comment"), d["revision"])])

    @extend_schema(request=BatchSerializer, responses=None)
    @action(detail=True, methods=["post"], url_path="save-draft")
    def save_draft(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        execution = self.get_object()
        payload = BatchSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        inputs = [AnswerInput(str(r["item"]), r.get("value"), r["not_applicable"], r.get("comment"), r["revision"]) for r in payload.validated_data["responses"]]
        return self._saved(request, execution, inputs)

    @extend_schema(request=None, responses=None)
    @action(detail=True, methods=["post"])
    def validate(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        """A dry run of submission: what would block it.  Always 200; never changes anything."""
        execution = self.get_object()
        if not policies.can_view(request.user, execution):
            raise PermissionDenied
        state, problems = services.review(execution)
        return Response({"valid": not problems, "problems": services.describe(problems, state), "progress": services.compute_progress(state)})

    @extend_schema(request=None, responses=ExecutionSerializer)
    @action(detail=True, methods=["post"])
    def submit(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        execution = self.get_object()
        result = run(lambda: services.submit(execution, actor=request.user))
        data = dict(self.get_serializer(_fresh(request.user, result.execution)).data)
        data["already_submitted"] = result.already_submitted
        return Response(data)

    @extend_schema(request=CancelSerializer, responses=ExecutionSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        execution = self.get_object()
        payload = CancelSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        run(lambda: services.cancel_execution(execution, actor=request.user, reason=payload.validated_data["reason"]))
        return self._out(execution)


class MyAssignmentsView(generics.ListAPIView):
    """Open work the caller may execute (their own, or the undesignated work of their locations)."""

    serializer_class = MyAssignmentSerializer
    permission_classes = [IsActiveUser]

    def get_queryset(self):  # noqa: ANN201
        from django.db.models import Prefetch

        live = Prefetch("executions", queryset=ChecklistExecution.objects.filter(status__in=("in_progress", "submitted")), to_attr="live_executions")
        return selectors.my_assignments(self.request.user).prefetch_related(live).order_by("due_at", "-created_at")


class AssignmentExecutionView(APIView):
    """``GET /assignments/{id}/execution/`` - the assignment's live execution (404 when there is none or it is not visible)."""

    permission_classes = [IsActiveUser]

    def get(self, request: Request, pk) -> Response:  # noqa: ANN001
        assignment = selectors.assignment_for_work(request.user, pk)
        execution = selectors.execution_for_assignment(request.user, assignment) if assignment else None
        if execution is None:
            raise NotFound
        return Response(ExecutionSerializer(execution).data)


class StartExecutionView(APIView):
    """``POST /assignments/{id}/start-execution/`` - idempotent: 201 on the first call, 200 when it resumes the existing execution."""

    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(request=None, responses=ExecutionSerializer)
    def post(self, request: Request, pk) -> Response:  # noqa: ANN001
        if request.data:
            raise serializers.ValidationError({k: "This endpoint takes no body." for k in request.data})
        assignment = selectors.assignment_for_work(request.user, pk)
        if assignment is None:
            raise NotFound
        execution, created = run(lambda: services.start_or_resume(assignment, actor=request.user))
        data = ExecutionSerializer(selectors.executions_visible_to(request.user).get(pk=execution.pk)).data
        return Response(data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

