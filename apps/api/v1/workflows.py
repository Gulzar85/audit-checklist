"""``/api/v1/workflows/``, ``workflow-versions/``, ``workflow-instances/``, ``workflow-tasks/`` (Phase 14).

Thin adapters like every v1 resource: querysets are the workflow selectors (organization isolation + scope: invisible -> 404), authorization is
``apps.workflows.policies`` (403) and every write goes through ``apps.workflows.services``.  There is no endpoint that sets a state, a task status, a target
model or an actor: transitions name an *action code*; the server selects the branch, checks the actor and runs the registered business action.  Unknown fields
are rejected, not ignored.  ``POST .../transitions/`` is idempotent per ``idempotency_key`` (body or ``Idempotency-Key`` header).
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.exceptions import ConflictAPIException, StructuredValidationError
from apps.api.permissions import IsActiveUser
from apps.checklists.rules.operators import OPERATORS
from apps.core.utils import parse_uuid
from apps.workflows import conditions, handlers, policies, selectors, services, targets, validation
from apps.workflows.constants import SEPARATION_OPTIONS, ActorRule, ApprovalMode, Decision, OnNoAssignee, StateKind
from apps.workflows.models import WorkflowDefinition, WorkflowInstance, WorkflowState, WorkflowTask, WorkflowTransition, WorkflowVersion

NOT_FOUND_CODES = {"target_not_found"}


def _raise(exc: DjangoValidationError) -> None:
    code = getattr(exc, "workflow_code", "workflow_error")
    if isinstance(exc, services.WorkflowConflict):
        raise ConflictAPIException(detail={"detail": " ".join(exc.messages), "code": code}) from exc
    if isinstance(exc, services.WorkflowValidationError):
        raise StructuredValidationError({"__all__": exc.report.errors}, extra={"code": code, "report": exc.report.as_dict()}) from exc
    if code in NOT_FOUND_CODES:
        raise NotFound(" ".join(exc.messages)) from exc
    raise StructuredValidationError({"__all__": list(exc.messages)}, extra={"code": code}) from exc


def run(call):  # noqa: ANN001, ANN201
    try:
        return call()
    except DjangoPermissionDenied as exc:
        raise PermissionDenied from exc
    except DjangoValidationError as exc:
        _raise(exc)


class _Strict(serializers.Serializer):
    def to_internal_value(self, data):  # noqa: ANN001, ANN201
        unknown = {k: "This field cannot be set." for k in data if k not in self.fields}
        if unknown:
            raise serializers.ValidationError(unknown)
        return super().to_internal_value(data)


# -- output ---------------------------------------------------------------------------------------------------------------
def definition_out(d: WorkflowDefinition) -> dict:
    return {"id": str(d.pk), "organization": str(d.organization_id) if d.organization_id else None, "system": d.is_system, "code": d.code, "name": d.name,
            "description": d.description, "target_type": d.target_type, "is_default": d.is_default, "status": d.status, "auto_start_event": d.auto_start_event,
            "created_at": d.created_at, "versions": [version_brief(v) for v in d.versions.all()]}


def version_brief(v: WorkflowVersion) -> dict:
    return {"id": str(v.pk), "number": v.number, "status": v.status, "change_summary": v.change_summary, "published_at": v.published_at, "config_hash": v.config_hash}


def state_out(s: WorkflowState) -> dict:
    return {"id": str(s.pk), "code": s.code, "name": s.name, "description": s.description, "kind": s.kind, "sort_order": s.sort_order, "assignee_policy": s.assignee_policy,
            "approval_mode": s.approval_mode, "min_approvals": s.min_approvals, "separation": s.separation, "due_hours": s.due_hours, "on_no_assignee": s.on_no_assignee}


def transition_out(t: WorkflowTransition) -> dict:
    return {"id": str(t.pk), "action": t.action, "label": t.label, "source": t.source.code, "target": t.target.code, "handler": t.handler, "actor_rule": t.actor_rule,
            "decision": t.decision, "reason_required": t.reason_required, "condition": t.condition, "sort_order": t.sort_order, "enabled": t.enabled}


def version_out(v: WorkflowVersion) -> dict:
    return {**version_brief(v), "definition": str(v.definition_id), "workflow": v.definition.code, "target_type": v.definition.target_type, "start_condition": v.start_condition,
            "allow_loops": v.allow_loops, "states": [state_out(s) for s in v.states.order_by("sort_order", "code")],
            "transitions": [transition_out(t) for t in v.transitions.select_related("source", "target").order_by("source__sort_order", "sort_order", "action")]}


def instance_out(i: WorkflowInstance) -> dict:
    spec = targets.get_spec(i.target_type)
    return {"id": str(i.pk), "reference": i.reference, "organization": str(i.organization_id), "workflow": {"id": str(i.definition_id), "code": i.definition.code, "name": i.definition.name},
            "version": {"id": str(i.version_id), "number": i.version.number}, "target_type": i.target_type, "target_id": str(i.target_id), "target_label": spec.label,
            "location": {"id": str(i.location_id), "name": i.location.name}, "state": {"code": i.state.code, "name": i.state.name, "kind": i.state.kind},
            "status": i.status, "outcome": i.outcome, "needs_attention": i.needs_attention, "revision": i.revision, "started_at": i.started_at, "completed_at": i.completed_at,
            "started_by": str(i.started_by_id) if i.started_by_id else None, "system_started": i.system_started, "cancelled_at": i.cancelled_at, "cancel_reason": i.cancel_reason}


def task_out(t: WorkflowTask) -> dict:
    return {"id": str(t.pk), "instance": str(t.instance_id), "reference": t.instance.reference, "workflow": t.instance.definition.code, "state": {"code": t.state.code, "name": t.state.name},
            "status": t.status, "assignee": {"id": str(t.assignee_id), "email": t.assignee.email} if t.assignee_id else None, "claimable": t.slot == "group" and t.assignee_id is None,
            "due_at": t.due_at, "is_overdue": t.is_overdue, "revision": t.revision, "created_at": t.created_at, "completed_at": t.completed_at}


def event_out(e) -> dict:  # noqa: ANN001
    return {"seq": e.seq, "kind": e.kind, "action": e.action, "from_state": e.from_state.code if e.from_state_id else None, "to_state": e.to_state.code if e.to_state_id else None,
            "actor": e.actor.email if e.actor_id else None, "comment": e.comment, "task": str(e.task_id) if e.task_id else None, "metadata": e.metadata, "created_at": e.created_at}


# -- input ----------------------------------------------------------------------------------------------------------------
class DefinitionCreate(_Strict):
    organization = serializers.UUIDField(required=False, allow_null=True)
    code = serializers.CharField(max_length=60)
    name = serializers.CharField(max_length=120)
    description = serializers.CharField(required=False, allow_blank=True, max_length=500)
    target_type = serializers.CharField(max_length=40)
    is_default = serializers.BooleanField(required=False, default=False)
    auto_start_event = serializers.CharField(required=False, allow_blank=True, max_length=60)


class DefinitionPatch(_Strict):
    name = serializers.CharField(max_length=120, required=False)
    description = serializers.CharField(required=False, allow_blank=True, max_length=500)
    is_default = serializers.BooleanField(required=False)
    auto_start_event = serializers.CharField(required=False, allow_blank=True, max_length=60)


class DraftCreate(_Strict):
    change_summary = serializers.CharField(required=False, allow_blank=True, max_length=300)
    clone_from = serializers.UUIDField(required=False)


class VersionPatch(_Strict):
    change_summary = serializers.CharField(required=False, allow_blank=True, max_length=300)
    start_condition = serializers.JSONField(required=False, allow_null=True)
    allow_loops = serializers.BooleanField(required=False)


class StateWrite(_Strict):
    code = serializers.CharField(required=False, max_length=40)
    name = serializers.CharField(required=False, max_length=120)
    description = serializers.CharField(required=False, allow_blank=True, max_length=300)
    kind = serializers.ChoiceField(choices=StateKind.choices, required=False)
    sort_order = serializers.IntegerField(required=False)
    assignee_policy = serializers.JSONField(required=False, allow_null=True)
    approval_mode = serializers.ChoiceField(choices=ApprovalMode.choices, required=False)
    min_approvals = serializers.IntegerField(required=False)
    separation = serializers.ListField(child=serializers.CharField(), required=False)
    due_hours = serializers.IntegerField(required=False, allow_null=True)
    on_no_assignee = serializers.ChoiceField(choices=OnNoAssignee.choices, required=False)


class TransitionWrite(_Strict):
    action = serializers.CharField(required=False, max_length=40)
    label = serializers.CharField(required=False, max_length=80)
    source = serializers.CharField(required=False, max_length=40)
    target = serializers.CharField(required=False, max_length=40)
    handler = serializers.CharField(required=False, allow_blank=True, max_length=60)
    actor_rule = serializers.ChoiceField(choices=ActorRule.choices, required=False)
    decision = serializers.ChoiceField(choices=Decision.choices, required=False)
    reason_required = serializers.BooleanField(required=False)
    condition = serializers.JSONField(required=False, allow_null=True)
    sort_order = serializers.IntegerField(required=False)
    enabled = serializers.BooleanField(required=False)


class StartSerializer(_Strict):
    target_type = serializers.CharField(max_length=40)
    target_id = serializers.CharField(max_length=64)
    workflow = serializers.CharField(required=False, allow_blank=True, max_length=60)


class PerformSerializer(_Strict):
    action = serializers.CharField(max_length=40)
    comment = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    idempotency_key = serializers.UUIDField(required=False)
    expected_revision = serializers.IntegerField(required=False, min_value=1)


class ReasonSerializer(_Strict):
    reason = serializers.CharField(max_length=300)


class ReassignSerializer(_Strict):
    assignee = serializers.UUIDField()


# ---------------------------------------------------------------------------------------------------------------------------------
class MetadataView(APIView):
    """The controlled vocabularies a designer may choose from (target types and their attributes / events, business actions, operators, options)."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses=None)
    def get(self, request: Request) -> Response:
        return Response({"target_types": targets.describe_all(), "handlers": handlers.describe_all(), "operators": [op.describe() for op in OPERATORS.values()],
                         "separation_options": SEPARATION_OPTIONS, "state_kinds": [k for k, _l in StateKind.choices], "actor_rules": [k for k, _l in ActorRule.choices],
                         "decisions": [k for k, _l in Decision.choices], "approval_modes": [k for k, _l in ApprovalMode.choices]})


class WorkflowViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsActiveUser]
    http_method_names = ["get", "post", "patch", "head", "options"]
    search_fields = ("code", "name")
    ordering = ("name",)

    def get_queryset(self):  # noqa: ANN201
        return selectors.definitions_visible_to(self.request.user).prefetch_related("versions")

    def list(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        return self.get_paginated_response([definition_out(d) for d in page])

    def retrieve(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        return Response(definition_out(self.get_object()))

    @extend_schema(request=DefinitionCreate, responses=None)
    def create(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        payload = DefinitionCreate(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        from apps.organizations.models import Organization

        org = None
        if d.get("organization"):
            org = Organization.objects.filter(pk=d["organization"]).first()
            if org is None or not policies.can_create(request.user, org):
                raise PermissionDenied if org is not None else serializers.ValidationError({"organization": "Unknown organization."})
        created = run(lambda: services.create_definition(actor=request.user, organization=org, code=d["code"], name=d["name"], target_type=d["target_type"],
                                                         description=d.get("description", ""), is_default=d["is_default"], auto_start_event=d.get("auto_start_event", "")))
        return Response(definition_out(created), status=status.HTTP_201_CREATED)

    @extend_schema(request=DefinitionPatch, responses=None)
    def partial_update(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        obj = self.get_object()
        payload = DefinitionPatch(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        updated = run(lambda: services.update_definition(obj, actor=request.user, **payload.validated_data))
        return Response(definition_out(updated))

    def update(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        if not kwargs.get("partial"):
            from rest_framework.exceptions import MethodNotAllowed

            raise MethodNotAllowed("PUT")
        return self.partial_update(request, *args, **kwargs)

    @extend_schema(request=DraftCreate, responses=None)
    @action(detail=True, methods=["get", "post"])
    def versions(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        obj = self.get_object()
        if request.method == "GET":
            return Response({"results": [version_brief(v) for v in obj.versions.all()]})
        payload = DraftCreate(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        source = obj.versions.filter(pk=d["clone_from"]).first() if d.get("clone_from") else None
        if d.get("clone_from") and source is None:
            raise serializers.ValidationError({"clone_from": "Unknown version."})
        draft = run(lambda: services.create_draft(obj, actor=request.user, clone_from=source, change_summary=d.get("change_summary", "")))
        return Response(version_out(draft), status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses=None)
    @action(detail=True, methods=["post"])
    def archive(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return Response(definition_out(run(lambda: services.archive_definition(self.get_object(), actor=request.user))))


class WorkflowVersionViewSet(mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsActiveUser]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):  # noqa: ANN201
        return selectors.versions_visible_to(self.request.user)

    def retrieve(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        return Response(version_out(self.get_object()))

    @extend_schema(request=VersionPatch, responses=None)
    def partial_update(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        obj = self.get_object()
        payload = VersionPatch(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        return Response(version_out(run(lambda: services.update_version(obj, actor=request.user, **payload.validated_data))))

    def update(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        if not kwargs.get("partial"):
            from rest_framework.exceptions import MethodNotAllowed

            raise MethodNotAllowed("PUT")
        return self.partial_update(request, *args, **kwargs)

    @extend_schema(request=None, responses=None)
    @action(detail=True, methods=["post"])
    def validate(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return Response(run(lambda: services.validate_version(self.get_object(), actor=request.user)).as_dict())

    @extend_schema(request=None, responses=None)
    @action(detail=True, methods=["post"])
    def publish(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return Response(version_out(run(lambda: services.publish(self.get_object(), actor=request.user))))

    @extend_schema(request=None, responses=None)
    @action(detail=True, methods=["post"])
    def clone(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        obj = self.get_object()
        return Response(version_out(run(lambda: services.create_draft(obj.definition, actor=request.user, clone_from=obj))), status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses=None)
    @action(detail=True, methods=["post"])
    def archive(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return Response(version_out(run(lambda: services.archive_version(self.get_object(), actor=request.user))))

    @extend_schema(responses=None)
    @action(detail=True, methods=["get"])
    def preview(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        """A readable map: each state with the transitions leaving it (conditions in plain text), plus the validation report."""
        v = self.get_object()
        spec_type = v.definition.target_type
        states = []
        for s in v.states.order_by("sort_order", "code"):
            states.append({**state_out(s), "outgoing": [{**transition_out(t), "condition_text": conditions.describe(spec_type, t.condition)}
                                                        for t in v.transitions.filter(source=s).select_related("source", "target").order_by("sort_order", "action")]})
        return Response({"version": version_brief(v), "start_condition_text": conditions.describe(spec_type, v.start_condition), "states": states, "validation": validation.validate(v).as_dict()})

    @extend_schema(request=StateWrite, responses=None)
    @action(detail=True, methods=["get", "post"])
    def states(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        v = self.get_object()
        if request.method == "GET":
            return Response({"results": [state_out(s) for s in v.states.order_by("sort_order", "code")]})
        payload = StateWrite(data=request.data)
        payload.is_valid(raise_exception=True)
        return Response(state_out(run(lambda: services.add_state(v, actor=request.user, **payload.validated_data))), status=status.HTTP_201_CREATED)

    @extend_schema(request=StateWrite, responses=None)
    @action(detail=True, methods=["patch", "delete"], url_path=r"states/(?P<code>[a-z0-9_-]+)")
    def state(self, request: Request, pk=None, code=None) -> Response:  # noqa: ANN001
        v = self.get_object()
        s = v.states.filter(code=code).first()
        if s is None:
            raise NotFound
        if request.method == "DELETE":
            run(lambda: services.delete_state(s, actor=request.user))
            return Response(status=status.HTTP_204_NO_CONTENT)
        payload = StateWrite(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        return Response(state_out(run(lambda: services.update_state(s, actor=request.user, **payload.validated_data))))

    @extend_schema(request=TransitionWrite, responses=None)
    @action(detail=True, methods=["get", "post"])
    def transitions(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        v = self.get_object()
        if request.method == "GET":
            return Response({"results": [transition_out(t) for t in v.transitions.select_related("source", "target").order_by("source__sort_order", "sort_order")]})
        payload = TransitionWrite(data=request.data)
        payload.is_valid(raise_exception=True)
        return Response(transition_out(run(lambda: services.add_transition(v, actor=request.user, **payload.validated_data))), status=status.HTTP_201_CREATED)

    @extend_schema(request=TransitionWrite, responses=None)
    @action(detail=True, methods=["patch", "delete"], url_path=r"transitions/(?P<transition_id>[0-9a-f-]{36})")
    def transition(self, request: Request, pk=None, transition_id=None) -> Response:  # noqa: ANN001
        v = self.get_object()
        t = v.transitions.select_related("source", "target").filter(pk=transition_id).first()
        if t is None:
            raise NotFound
        if request.method == "DELETE":
            run(lambda: services.delete_transition(t, actor=request.user))
            return Response(status=status.HTTP_204_NO_CONTENT)
        payload = TransitionWrite(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        return Response(transition_out(run(lambda: services.update_transition(t, actor=request.user, **payload.validated_data))))


class WorkflowInstanceViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsActiveUser]
    http_method_names = ["get", "post", "head", "options"]
    ordering = ("-started_at",)

    def get_queryset(self):  # noqa: ANN201
        qs = selectors.instances_visible_to(self.request.user)
        for param, field in (("status", "status"), ("target_type", "target_type"), ("target_id", "target_id"), ("workflow", "definition__code")):
            value = self.request.query_params.get(param)
            if value:
                if param == "target_id" and parse_uuid(value) is None:
                    return qs.none()
                qs = qs.filter(**{field: value})
        return qs

    def list(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        return self.get_paginated_response([instance_out(i) for i in page])

    def retrieve(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        i = self.get_object()
        return Response({**instance_out(i), "available_transitions": services.available_transitions(i, request.user),
                         "tasks": [task_out(t) for t in i.tasks.select_related("instance__definition", "state", "assignee").filter(entry_seq=i.entry_seq)]})

    @extend_schema(request=StartSerializer, responses=None)
    def create(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        payload = StartSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        created = run(lambda: services.start(actor=request.user, target_type=d["target_type"], target=d["target_id"], definition_code=d.get("workflow") or None))
        return Response(instance_out(selectors.get_instance(request.user, created.pk) or created), status=status.HTTP_201_CREATED)

    @extend_schema(responses=None)
    @action(detail=True, methods=["get"])
    def history(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return Response({"results": [event_out(e) for e in selectors.history(self.get_object())]})

    @extend_schema(responses=None)
    @action(detail=True, methods=["get"], url_path="available-transitions")
    def available(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        i = self.get_object()
        return Response({"revision": i.revision, "results": services.available_transitions(i, request.user)})

    @extend_schema(request=PerformSerializer, responses=None)
    @action(detail=True, methods=["post"])
    def transitions(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        i = self.get_object()
        payload = PerformSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        key = d.get("idempotency_key") or parse_uuid(request.headers.get("Idempotency-Key"))
        result = run(lambda: services.perform(i, d["action"], actor=request.user, comment=d.get("comment", ""), idempotency_key=key, expected_revision=d.get("expected_revision")))
        fresh = selectors.instances_visible_to(request.user).filter(pk=i.pk).first() or result.instance
        return Response({**instance_out(fresh), "replayed": result.replayed, "pending_approvals": result.pending_approvals, "handler_outcome": result.outcome},
                        status=status.HTTP_200_OK)

    @extend_schema(request=ReasonSerializer, responses=None)
    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        i = self.get_object()
        payload = ReasonSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return Response(instance_out(run(lambda: services.cancel(i, actor=request.user, reason=payload.validated_data["reason"]))))


class WorkflowTaskViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsActiveUser]
    http_method_names = ["get", "post", "head", "options"]
    ordering = ("due_at", "created_at")

    def get_queryset(self):  # noqa: ANN201
        qs = selectors.my_tasks(self.request.user) if self.action == "mine" else selectors.tasks_visible_to(self.request.user)
        for param, field in (("status", "status"), ("instance", "instance_id")):
            value = self.request.query_params.get(param)
            if value:
                qs = qs.filter(**{field: value}) if param == "status" or parse_uuid(value) else qs.none()
        if self.request.query_params.get("overdue") in ("1", "true"):
            qs = selectors.overdue_tasks(qs)
        return qs

    def list(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        return self.get_paginated_response([task_out(t) for t in page])

    def retrieve(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        return Response(task_out(self.get_object()))

    @extend_schema(responses=None)
    @action(detail=False, methods=["get"])
    def mine(self, request: Request) -> Response:
        return self.list(request)

    @extend_schema(responses=None)
    @action(detail=False, methods=["get"])
    def claimable(self, request: Request) -> Response:
        return Response({"results": [task_out(t) for t in selectors.claimable_for(request.user)]})

    @extend_schema(responses=None)
    @action(detail=False, methods=["get"])
    def summary(self, request: Request) -> Response:
        return Response(selectors.task_summary(request.user))

    def _task(self, request: Request) -> WorkflowTask:
        """Claiming needs a task the caller can *see or could claim*: a claimable task is visible to the users eligible for it."""
        task = self.get_queryset().filter(pk=self.kwargs["pk"]).first() if parse_uuid(self.kwargs["pk"]) else None
        if task is None:
            task = next((t for t in selectors.claimable_for(request.user) if str(t.pk) == str(self.kwargs["pk"])), None)
        if task is None:
            raise NotFound
        return task

    @extend_schema(request=None, responses=None)
    @action(detail=True, methods=["post"])
    def claim(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return Response(task_out(run(lambda: services.claim(self._task(request), actor=request.user))))

    @extend_schema(request=None, responses=None)
    @action(detail=True, methods=["post"])
    def release(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return Response(task_out(run(lambda: services.release(self._task(request), actor=request.user))))

    @extend_schema(request=ReassignSerializer, responses=None)
    @action(detail=True, methods=["post"])
    def reassign(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        from django.contrib.auth import get_user_model

        task = self._task(request)
        payload = ReassignSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        assignee = get_user_model().objects.filter(pk=payload.validated_data["assignee"]).first()
        if assignee is None:
            raise serializers.ValidationError({"assignee": "Unknown user."})
        return Response(task_out(run(lambda: services.reassign(task, actor=request.user, assignee=assignee))))
