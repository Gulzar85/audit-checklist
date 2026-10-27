"""``/api/v1/scheduling/…`` (Phase 18): schedules, jobs, run-now, cancel / retry and diagnostics.

Thin adapters over ``apps.jobs``: visibility from the selectors (an invisible schedule or job is a 404), authorization and every state change from the services (403 / 409) -
the same code the web screens use.  There is no endpoint that sets a status, a result or an attempt, nor one that accepts a task identifier outside the server-side registry.
Payloads of jobs are never returned (they may hold settings), nor are tokens, leases or tracebacks.  Unknown request fields are rejected.  Run-now accepts an
``Idempotency-Key`` header: repeating the request returns the first job instead of queuing another.  The standard per-user API throttle applies to every call.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.context import get_active_organization
from apps.api.exceptions import ConflictAPIException, StructuredValidationError
from apps.api.permissions import IsActiveUser
from apps.jobs import constants as k
from apps.jobs import policies, recurrence, registry, selectors, services
from apps.jobs.models import ScheduleDefinition, ScheduledJob

PAGE_MAX = 100


def run_service(call):  # noqa: ANN001, ANN201
    try:
        return call()
    except DjangoPermissionDenied as exc:
        raise PermissionDenied from exc
    except services.JobConflict as exc:
        raise ConflictAPIException(detail={"detail": exc.message, "code": exc.code}) from exc
    except services.JobError as exc:
        raise StructuredValidationError({"__all__": [exc.message]}, extra={"code": exc.code, **exc.extra}) from exc


class _Strict(serializers.Serializer):
    def to_internal_value(self, data):  # noqa: ANN001, ANN201
        unknown = {key: "This field cannot be set." for key in data if key not in self.fields}
        if unknown:
            raise serializers.ValidationError(unknown)
        return super().to_internal_value(data)


def _page(request: Request, qs):  # noqa: ANN001, ANN202
    try:
        limit = max(1, min(int(request.query_params.get("limit", 25)), PAGE_MAX))
        offset = max(0, int(request.query_params.get("offset", 0)))
    except ValueError as exc:
        raise StructuredValidationError({"limit": ["Use whole numbers for limit and offset."]}) from exc
    return qs.count(), list(qs[offset:offset + limit])


def _schedule(s: ScheduleDefinition) -> dict[str, Any]:
    return {"id": str(s.pk), "name": s.name, "description": s.description, "task_type": s.task_type, "schedule_type": s.schedule_type, "status": s.status, "timezone": s.timezone,
            "start_at": s.start_at, "end_at": s.end_at, "recurrence": s.recurrence, "summary": recurrence.describe(s.schedule_type, s.recurrence, s.timezone), "payload": s.payload,
            "misfire_policy": s.misfire_policy, "max_attempts": s.max_attempts, "next_run_at": s.next_run_at, "last_run_at": s.last_run_at, "revision": s.revision,
            "organization": str(s.organization_id) if s.organization_id else None, "platform_wide": s.organization_id is None}


def _job(j: ScheduledJob, *, detail: bool = False) -> dict[str, Any]:
    data = {"id": str(j.pk), "task_type": j.task_type, "status": j.status, "trigger": j.trigger, "schedule": str(j.schedule_id) if j.schedule_id else None, "scheduled_for": j.scheduled_for,
            "available_at": j.available_at, "attempt_count": j.attempt_count, "max_attempts": j.max_attempts, "started_at": j.started_at, "finished_at": j.finished_at, "duration_ms": j.duration_ms,
            "error_category": j.error_category, "error_message": j.error_message, "correlation_id": j.correlation_id, "result": j.result,
            "organization": str(j.organization_id) if j.organization_id else None}
    if detail:
        data["executions"] = [{"number": e.number, "outcome": e.outcome, "started_at": e.started_at, "finished_at": e.finished_at, "duration_ms": e.duration_ms, "error_category": e.error_category,
                               "error_message": e.error_message, "retry_decision": e.retry_decision} for e in selectors.executions_of(j)]
    return data


class ScheduleIn(_Strict):
    name = serializers.CharField(max_length=120)
    description = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    task_type = serializers.CharField(max_length=60)
    schedule_type = serializers.ChoiceField(choices=k.ScheduleType.choices)
    timezone = serializers.CharField(max_length=64, required=False, default="UTC")
    start_at = serializers.DateTimeField()
    end_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    recurrence = serializers.DictField(required=False, default=dict)
    payload = serializers.DictField(required=False, default=dict)
    misfire_policy = serializers.ChoiceField(choices=k.MisfirePolicy.choices, required=False, default=k.MisfirePolicy.SKIP)
    max_attempts = serializers.IntegerField(required=False, default=3)
    platform_wide = serializers.BooleanField(required=False, default=False)


class ScheduleUpdateIn(ScheduleIn):
    allow_timezone_change = serializers.BooleanField(required=False, default=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.required = False
            field.default = serializers.empty


class ManualRunIn(_Strict):
    task_type = serializers.CharField(max_length=60)
    payload = serializers.DictField(required=False, default=dict)
    delay_seconds = serializers.IntegerField(required=False, default=0)
    platform_wide = serializers.BooleanField(required=False, default=False)


class ReasonIn(_Strict):
    reason = serializers.CharField(max_length=300)


class TaskListView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["scheduling"])
    def get(self, request: Request) -> Response:
        org = get_active_organization(request._request)
        rows = [{**registry.describe(t), "can_schedule": policies.can_schedule(request.user, t, org if t.scope != registry.SCOPE_GLOBAL else None),
                 "can_trigger": policies.can_trigger(request.user, t, org if t.scope != registry.SCOPE_GLOBAL else None)} for t in registry.REGISTRY.values()]
        return Response({"tasks": rows, "schedule_types": [{"value": v, "label": str(label)} for v, label in k.ScheduleType.choices], "misfire_policies": [{"value": v, "label": str(label)} for v, label in k.MisfirePolicy.choices]})


class ScheduleListView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["scheduling"])
    def get(self, request: Request) -> Response:
        qs = selectors.schedules_visible_to(request.user).order_by("name")
        params = request.query_params
        if params.get("status") in k.ScheduleStatus.values:
            qs = qs.filter(status=params["status"])
        if params.get("task_type") in registry.REGISTRY:
            qs = qs.filter(task_type=params["task_type"])
        if params.get("q"):
            qs = qs.filter(name__icontains=params["q"][:80])
        total, rows = _page(request, qs)
        return Response({"count": total, "results": [_schedule(s) for s in rows]})

    @extend_schema(request=ScheduleIn, responses={201: dict}, tags=["scheduling"])
    def post(self, request: Request) -> Response:
        data = ScheduleIn(data=request.data)
        data.is_valid(raise_exception=True)
        body = dict(data.validated_data)
        platform = body.pop("platform_wide")
        org = None if platform else get_active_organization(request._request)
        return Response(_schedule(run_service(lambda: services.save_schedule(request.user, org, body))), status=201)


class ScheduleDetailView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["scheduling"])
    def get(self, request: Request, pk: str) -> Response:
        s = selectors.get_schedule(request.user, pk)
        if s is None:
            raise NotFound
        return Response(_schedule(s))

    @extend_schema(request=ScheduleUpdateIn, responses={200: dict}, tags=["scheduling"])
    def put(self, request: Request, pk: str) -> Response:
        s = selectors.get_schedule(request.user, pk)
        if s is None:
            raise NotFound
        data = ScheduleUpdateIn(data=request.data)
        data.is_valid(raise_exception=True)
        body = dict(data.validated_data)
        body.pop("platform_wide", None)
        confirm = bool(body.pop("allow_timezone_change", False))
        return Response(_schedule(run_service(lambda: services.save_schedule(request.user, None, body, schedule=s, allow_timezone_change=confirm))))


class ScheduleActionView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(request=None, responses={200: dict}, tags=["scheduling"])
    def post(self, request: Request, pk: str, action: str) -> Response:
        s = selectors.get_schedule(request.user, pk)
        if s is None or action not in {"enable", "disable", "pause", "resume", "run"}:
            raise NotFound
        if request.data:
            raise StructuredValidationError({"__all__": ["This request takes no body."]})
        if action == "run":
            key = request.headers.get("Idempotency-Key", "")[:60]
            job = run_service(lambda: services.run_now(s, request.user, client_key=key))
            return Response(_job(job), status=202)
        return Response(_schedule(run_service(lambda: services.set_status(s, request.user, action))))


class UpcomingView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["scheduling"])
    def get(self, request: Request) -> Response:
        qs = selectors.schedules_visible_to(request.user).filter(status=k.ScheduleStatus.ACTIVE, next_run_at__isnull=False).order_by("next_run_at")
        total, rows = _page(request, qs)
        return Response({"count": total, "results": [{"schedule": str(s.pk), "name": s.name, "task_type": s.task_type, "next_run_at": s.next_run_at, "timezone": s.timezone} for s in rows]})


class JobListView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["scheduling"])
    def get(self, request: Request) -> Response:
        qs = selectors.jobs_visible_to(request.user).order_by("-scheduled_for")
        params = request.query_params
        if params.get("status") in k.JobStatus.values:
            qs = qs.filter(status=params["status"])
        if params.get("task_type") in registry.REGISTRY:
            qs = qs.filter(task_type=params["task_type"])
        if params.get("schedule"):
            qs = qs.filter(schedule_id=params["schedule"]) if len(params["schedule"]) == 36 else qs.none()
        total, rows = _page(request, qs)
        return Response({"count": total, "results": [_job(j) for j in rows]})

    @extend_schema(request=ManualRunIn, responses={202: dict}, tags=["scheduling"])
    def post(self, request: Request) -> Response:
        """Run a registered task now (or after ``delay_seconds``).  Repeating the request with the same ``Idempotency-Key`` returns the first job."""
        data = ManualRunIn(data=request.data)
        data.is_valid(raise_exception=True)
        body = data.validated_data
        org = None if body["platform_wide"] else get_active_organization(request._request)
        key = request.headers.get("Idempotency-Key", "")[:60]
        job = run_service(lambda: services.create_manual(request.user, body["task_type"], org, body["payload"], delay_seconds=body["delay_seconds"], client_key=key))
        return Response(_job(job), status=202)


class JobDetailView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["scheduling"])
    def get(self, request: Request, pk: str) -> Response:
        j = selectors.get_job(request.user, pk)
        if j is None:
            raise NotFound
        return Response(_job(j, detail=True))


class JobActionView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(request=ReasonIn, responses={200: dict}, tags=["scheduling"])
    def post(self, request: Request, pk: str, action: str) -> Response:
        j = selectors.get_job(request.user, pk)
        call = {"cancel": services.cancel, "retry": services.retry}.get(action)
        if j is None or call is None:
            raise NotFound
        data = ReasonIn(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(_job(run_service(lambda: call(j, request.user, data.validated_data["reason"]))))


class DiagnosticsView(APIView):
    """Platform-wide: superusers only."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: dict}, tags=["scheduling"])
    def get(self, request: Request) -> Response:
        if not getattr(request.user, "is_superuser", False):
            raise PermissionDenied
        d = selectors.diagnostics(timezone.now())
        d["recent_success"] = [{"task_type": t, "finished_at": f} for t, f in d["recent_success"]]
        return Response(d)
