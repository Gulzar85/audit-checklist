"""``/api/v1/corrective-actions/`` - corrective actions (Phase 13).

A thin adapter like every v1 resource: the queryset is ``selectors.actions_visible_to`` (organization isolation + location scope, so an
invisible action is a 404), authorization is ``apps.corrective_actions.policies`` (403, including ownership and separation of duties) and
every write goes through ``apps.corrective_actions.services``.  There is no DELETE and no way to write ``status``, the verifier, the creator
or any verification data: the lifecycle is reachable only through the explicit action endpoints and PATCH accepts planning fields only
(unknown fields are rejected, not ignored).  Evidence uses the Phase 11 services.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from apps.api.exceptions import ConflictAPIException, StructuredValidationError
from apps.api.permissions import IsActiveUser, PolicyPermission
from apps.corrective_actions import eligibility, policies, selectors, services
from apps.corrective_actions.constants import TRANSITIONS, Priority, Purpose
from apps.corrective_actions.filters import ActionFilter
from apps.corrective_actions.models import CorrectiveAction
from apps.permissions.authorization import has_permission_anywhere
from apps.permissions.registry import P


def _raise(exc: DjangoValidationError) -> None:
    code = getattr(exc, "action_code", "action_error")
    if isinstance(exc, services.ActionConflict):
        raise ConflictAPIException(detail={"detail": " ".join(exc.messages), "code": code}) from exc
    raise StructuredValidationError({"__all__": list(exc.messages)}, extra={"code": code}) from exc


class _Strict(serializers.Serializer):
    """Reject unknown fields instead of silently ignoring them (``status``, ``verified_by``, ``organization``, ``finding`` on PATCH ...)."""

    def to_internal_value(self, data):  # noqa: ANN001, ANN201
        unknown = {k: "This field cannot be set." for k in data if k not in self.fields}
        if unknown:
            raise serializers.ValidationError(unknown)
        return super().to_internal_value(data)


# -- output ---------------------------------------------------------------------------------------------------------------
class ActionSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    reference = serializers.CharField(read_only=True)
    number = serializers.IntegerField(read_only=True)
    organization = serializers.UUIDField(source="organization_id", read_only=True)
    title = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    plan = serializers.CharField(read_only=True)
    expected_outcome = serializers.CharField(read_only=True)
    completion_criteria = serializers.CharField(read_only=True)
    priority = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    effective_status = serializers.CharField(read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    due_at = serializers.DateTimeField(read_only=True)
    required_evidence = serializers.IntegerField(read_only=True)
    independent_verification = serializers.BooleanField(read_only=True)
    blocker = serializers.CharField(read_only=True)
    blocked_at = serializers.DateTimeField(read_only=True)
    issued_at = serializers.DateTimeField(read_only=True)
    started_at = serializers.DateTimeField(read_only=True)
    submission_count = serializers.IntegerField(read_only=True)
    completion_summary = serializers.CharField(read_only=True)
    submitted_at = serializers.DateTimeField(read_only=True)
    verified_at = serializers.DateTimeField(read_only=True)
    closed_at = serializers.DateTimeField(read_only=True)
    cancelled_at = serializers.DateTimeField(read_only=True)
    cancel_reason = serializers.CharField(read_only=True)
    reopen_count = serializers.IntegerField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    created_by = serializers.UUIDField(source="created_by_id", read_only=True)
    submitted_by = serializers.UUIDField(source="submitted_by_id", read_only=True)
    verified_by = serializers.UUIDField(source="verified_by_id", read_only=True)
    assigned_to = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    finding = serializers.SerializerMethodField()

    def get_assigned_to(self, obj: CorrectiveAction) -> dict | None:
        u = obj.assigned_to
        return {"id": str(u.pk), "email": u.email, "name": u.display_name} if u else None

    def get_location(self, obj: CorrectiveAction) -> dict:
        return {"id": str(obj.location_id), "name": obj.location.name, "path": obj.location.display_path}

    def get_finding(self, obj: CorrectiveAction) -> dict:
        f = obj.finding
        return {"id": str(f.pk), "reference": f.reference, "title": f.title, "status": f.status,
                "severity": {"code": f.severity.code, "name": f.severity.name, "rank": f.severity.rank}}

    def to_representation(self, instance):  # noqa: ANN001, ANN201
        data = super().to_representation(instance)
        if self.context.get("detail"):
            request = self.context["request"]
            data["allowed_actions"] = [t.action for t in policies.available_actions(request.user, instance)]
            data["can_edit"], data["can_assign"] = policies.can_edit(request.user, instance), policies.can_assign(request.user, instance)
            data["can_add_update"] = policies.can_add_update(request.user, instance)
            data["finding_eligibility"] = eligibility.finding_eligibility(instance.finding).as_dict()
            data["verifications"] = [{"id": str(v.pk), "submission_number": v.submission_number, "decision": v.decision, "verifier": str(v.verifier_id) if v.verifier_id else None,
                                      "notes": v.notes, "reason": v.reason, "decided_at": v.decided_at} for v in instance.verifications.all()]
        return data


def _update_out(u) -> dict:  # noqa: ANN001
    return {"id": str(u.pk), "kind": u.kind, "body": u.body, "progress_percent": u.progress_percent, "submission_number": u.submission_number,
            "corrects": str(u.corrects_id) if u.corrects_id else None, "author": str(u.author_id) if u.author_id else None, "created_at": u.created_at}


# -- input ----------------------------------------------------------------------------------------------------------------
class ActionCreateSerializer(_Strict):
    finding = serializers.UUIDField()
    title = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    plan = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    expected_outcome = serializers.CharField(required=False, allow_blank=True, max_length=500)
    completion_criteria = serializers.CharField(required=False, allow_blank=True, max_length=500)
    priority = serializers.ChoiceField(choices=Priority.choices, required=False)
    assigned_to = serializers.UUIDField(required=False, allow_null=True)
    due_at = serializers.DateTimeField(required=False, allow_null=True)
    required_evidence = serializers.IntegerField(required=False, min_value=0, max_value=10)
    issue = serializers.BooleanField(required=False, default=False)


class NestedCreateSerializer(ActionCreateSerializer):
    finding = None  # the finding is the URL's


class ActionPatchSerializer(_Strict):
    title = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    plan = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    expected_outcome = serializers.CharField(required=False, allow_blank=True, max_length=500)
    completion_criteria = serializers.CharField(required=False, allow_blank=True, max_length=500)
    priority = serializers.ChoiceField(choices=Priority.choices, required=False)
    due_at = serializers.DateTimeField(required=False, allow_null=True)
    required_evidence = serializers.IntegerField(required=False, min_value=0, max_value=10)


class ReasonSerializer(_Strict):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class AssignSerializer(_Strict):
    assigned_to = serializers.UUIDField(allow_null=True)


class UpdateSerializer(_Strict):
    body = serializers.CharField(max_length=2000)
    progress_percent = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    corrects = serializers.UUIDField(required=False, allow_null=True)


class EvidenceUploadSerializer(serializers.Serializer):
    purpose = serializers.ChoiceField(choices=Purpose.choices)
    file = serializers.FileField()
    caption = serializers.CharField(required=False, allow_blank=True, max_length=200)
    operation_id = serializers.UUIDField(required=False)
    client_sha256 = serializers.RegexField(r"^[0-9a-fA-F]{64}$", required=False)


EVIDENCE_FIELDS = set(EvidenceUploadSerializer().fields)


def create_for_finding(request: Request, finding, data: dict):  # noqa: ANN001, ANN201
    """Shared by ``POST /corrective-actions/`` and ``POST /findings/{id}/corrective-actions/``."""
    from django.contrib.auth import get_user_model

    owner = None
    if data.get("assigned_to"):
        owner = get_user_model().objects.filter(pk=data["assigned_to"]).first()
        if owner is None:
            raise serializers.ValidationError({"assigned_to": "Unknown user."})
    try:
        return services.create_action(
            actor=request.user, finding=finding, title=data["title"], description=data.get("description", ""), plan=data.get("plan", ""),
            expected_outcome=data.get("expected_outcome", ""), completion_criteria=data.get("completion_criteria", ""), priority=data.get("priority", ""),
            assigned_to=owner, due_at=data.get("due_at"), required_evidence=data.get("required_evidence"), issue=data.get("issue", False))
    except DjangoPermissionDenied as exc:
        raise PermissionDenied from exc
    except DjangoValidationError as exc:
        _raise(exc)


class ActionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    serializer_class = ActionSerializer
    permission_classes = [IsActiveUser, PolicyPermission]
    filterset_class = ActionFilter
    search_fields = ("title", "finding__title", "location__name")
    ordering_fields = ("number", "due_at", "created_at", "updated_at", "status", "priority_order")
    ordering = ("-created_at",)
    http_method_names = ["get", "post", "patch", "head", "options"]  # no PUT, no DELETE

    #: HTTP action name -> lifecycle transition (``submit-for-verification`` is ``submit``).
    TRANSITION_OF = {"issue": "issue", "start": "start", "block": "block", "resume": "resume", "submit_for_verification": "submit", "verify": "verify",
                     "reject": "reject", "close": "close", "cancel": "cancel", "reopen": "reopen"}

    def get_queryset(self):  # noqa: ANN201
        if self.action == "verification_queue":
            return selectors.verification_queue(self.request.user)
        if self.action == "mine":
            return selectors.my_actions(self.request.user)
        return selectors.actions_visible_to(self.request.user)

    def get_serializer_context(self):  # noqa: ANN201
        return {**super().get_serializer_context(), "detail": self.action not in ("list", "mine", "verification_queue")}

    def policy_allows_collection(self, request: Request) -> bool:
        if self.action == "create":
            return has_permission_anywhere(request.user, P.CORRECTIVE_ACTION_CREATE)
        if self.action == "verification_queue":
            return has_permission_anywhere(request.user, P.CORRECTIVE_ACTION_VERIFY)
        return True

    def policy_allows_object(self, request: Request, obj: CorrectiveAction) -> bool:
        name = self.TRANSITION_OF.get(self.action)
        if name:
            return policies.can_act(request.user, obj, name)
        if self.action in ("assign", "reassign"):
            return policies._at(request.user, policies.assign_permission(obj), obj.location)
        return True  # reads; the services authorize every other write

    # -- helpers ------------------------------------------------------------------------------
    def _out(self, obj: CorrectiveAction, code: int = 200) -> Response:
        fresh = selectors.actions_visible_to(self.request.user).prefetch_related("verifications").get(pk=obj.pk)
        return Response(self.get_serializer(fresh).data, status=code)

    def _run(self, call):  # noqa: ANN001, ANN202
        try:
            return call()
        except DjangoPermissionDenied as exc:
            raise PermissionDenied from exc
        except DjangoValidationError as exc:
            _raise(exc)

    def _user(self, raw):  # noqa: ANN001, ANN202
        from django.contrib.auth import get_user_model

        if raw is None:
            return None
        found = get_user_model().objects.filter(pk=raw).first()
        if found is None:
            raise serializers.ValidationError({"assigned_to": "Unknown user."})
        return found

    # -- writes -------------------------------------------------------------------------------
    @extend_schema(request=ActionCreateSerializer, responses=ActionSerializer)
    def create(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        payload = ActionCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        from apps.findings import selectors as finding_selectors

        finding = finding_selectors.get_finding(request.user, payload.validated_data["finding"])
        if finding is None:
            raise serializers.ValidationError({"finding": "Unknown finding."})
        return self._out(create_for_finding(request, finding, payload.validated_data), status.HTTP_201_CREATED)

    @extend_schema(request=ActionPatchSerializer, responses=ActionSerializer)
    def partial_update(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        obj = self.get_object()
        payload = ActionPatchSerializer(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        self._run(lambda: services.update_action(obj, actor=request.user, **payload.validated_data))
        return self._out(obj)

    def update(self, request: Request, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        if not kwargs.get("partial"):
            from rest_framework.exceptions import MethodNotAllowed

            raise MethodNotAllowed("PUT")
        return self.partial_update(request, *args, **kwargs)

    def _transition(self, request: Request) -> Response:
        obj = self.get_object()
        payload = ReasonSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        self._run(lambda: services.transition(obj, self.TRANSITION_OF[self.action], actor=request.user, reason=payload.validated_data.get("reason", "")))
        return self._out(obj)

    @extend_schema(request=ReasonSerializer, responses=ActionSerializer)
    @action(detail=True, methods=["post"])
    def issue(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request)

    @extend_schema(request=ReasonSerializer, responses=ActionSerializer)
    @action(detail=True, methods=["post"])
    def start(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request)

    @extend_schema(request=ReasonSerializer, responses=ActionSerializer)
    @action(detail=True, methods=["post"])
    def block(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request)

    @extend_schema(request=ReasonSerializer, responses=ActionSerializer)
    @action(detail=True, methods=["post"])
    def resume(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request)

    @extend_schema(request=ReasonSerializer, responses=ActionSerializer)
    @action(detail=True, methods=["post"], url_path="submit-for-verification")
    def submit_for_verification(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request)

    @extend_schema(request=ReasonSerializer, responses=ActionSerializer)
    @action(detail=True, methods=["post"])
    def verify(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request)

    @extend_schema(request=ReasonSerializer, responses=ActionSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request)

    @extend_schema(request=ReasonSerializer, responses=ActionSerializer)
    @action(detail=True, methods=["post"])
    def close(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request)

    @extend_schema(request=ReasonSerializer, responses=ActionSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request)

    @extend_schema(request=ReasonSerializer, responses=ActionSerializer)
    @action(detail=True, methods=["post"])
    def reopen(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._transition(request)

    def _assign(self, request: Request) -> Response:
        obj = self.get_object()
        payload = AssignSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        owner = self._user(payload.validated_data["assigned_to"])
        self._run(lambda: services.assign(obj, actor=request.user, assigned_to=owner))
        return self._out(obj)

    @extend_schema(request=AssignSerializer, responses=ActionSerializer)
    @action(detail=True, methods=["post"])
    def assign(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._assign(request)

    @extend_schema(request=AssignSerializer, responses=ActionSerializer)
    @action(detail=True, methods=["post"])
    def reassign(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        return self._assign(request)

    # -- work log -----------------------------------------------------------------------------
    @extend_schema(request=UpdateSerializer, responses=None)
    @action(detail=True, methods=["get", "post"])
    def updates(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        obj = self.get_object()
        if request.method == "GET":
            return Response({"results": [_update_out(u) for u in obj.updates.order_by("created_at")]})
        payload = UpdateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        update = self._run(lambda: services.add_update(obj, actor=request.user, body=d["body"], progress_percent=d.get("progress_percent"),
                                                       kind="correction" if d.get("corrects") else "progress", corrects=d.get("corrects")))
        return Response(_update_out(update), status=status.HTTP_201_CREATED)

    @extend_schema(responses=None)
    @action(detail=True, methods=["get"])
    def history(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        obj = self.get_object()
        rows = [{"verb": e.verb, "description": e.description, "created_at": e.created_at, "actor": e.actor.email if e.actor else None,
                 "metadata": e.metadata} for e in selectors.timeline(obj)]
        return Response({"results": rows})

    # -- evidence -----------------------------------------------------------------------------
    @extend_schema(request=EvidenceUploadSerializer, responses=None)
    @action(detail=True, methods=["get", "post"], parser_classes=[MultiPartParser, FormParser, JSONParser])
    def evidence(self, request: Request, pk=None) -> Response:  # noqa: ANN001
        from apps.api.v1.evidence import run
        from apps.evidence import services as evidence_services
        from apps.evidence.selectors import is_servable

        obj = self.get_object()
        if request.method == "GET":
            base = f"/api/v1/corrective-actions/{obj.pk}/evidence"
            rows = [{"id": str(a.pk), "purpose": a.purpose, "name": a.asset.original_name, "kind": a.asset.kind, "content_type": a.asset.content_type,
                     "size": a.asset.size, "status": a.asset.status, "available": is_servable(a.asset), "caption": a.caption, "attached_at": a.created_at,
                     "detached_at": a.detached_at, "download_url": f"{base}/{a.pk}/download/" if is_servable(a.asset) else None}
                    for a in selectors.evidence_for(obj, include_detached=request.query_params.get("include_detached") in ("1", "true"))]
            return Response({"results": rows})
        if int(request.META.get("CONTENT_LENGTH") or 0) > int(settings.EVIDENCE_MAX_REQUEST_BYTES):
            raise StructuredValidationError({"file": ["The upload is too large."]}, extra={"code": "file_too_large"})
        unknown = sorted(set(request.data) - EVIDENCE_FIELDS)
        if unknown:
            raise StructuredValidationError({k: ["This field cannot be set."] for k in unknown}, extra={"code": "invalid_request"})
        payload = EvidenceUploadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        upload = d["file"]
        result = run(lambda: evidence_services.upload(
            request.user, target_type="corrective_action", ref={"corrective_action_id": str(obj.pk), "purpose": d["purpose"]}, filename=upload.name,
            data=upload.read(int(settings.EVIDENCE_MAX_FILE_BYTES) + 1), caption=d.get("caption", ""), operation_id=d.get("operation_id"),
            client_sha256=d.get("client_sha256", "")))
        a = result.attachment
        return Response({"id": str(a.pk), "purpose": a.purpose, "status": result.asset.status, "available": result.asset.is_available, "name": result.asset.original_name,
                         "duplicate": result.duplicate}, status=status.HTTP_200_OK if result.duplicate else status.HTTP_201_CREATED)

    def _file(self, request: Request, attachment: str, inline: bool):  # noqa: ANN202
        from apps.corrective_actions.views import ActionEvidenceFileView

        obj = self.get_object()
        view = ActionEvidenceFileView()
        view.request, view.kwargs = request._request, {"pk": obj.pk}
        view.inline = inline
        view._action = obj
        try:
            response = view.get(request._request, obj.pk, attachment)
        except Http404:
            raise
        response.accepted_renderer = None  # a raw file response: not wrapped in the JSON envelope
        return response

    @extend_schema(responses=None)
    @action(detail=True, methods=["get"], url_path=r"evidence/(?P<attachment>[0-9a-f-]{36})/download")
    def evidence_download(self, request: Request, pk=None, attachment=None):  # noqa: ANN001, ANN201
        return self._file(request, attachment, False)

    @extend_schema(responses=None)
    @action(detail=True, methods=["get"], url_path=r"evidence/(?P<attachment>[0-9a-f-]{36})/preview")
    def evidence_preview(self, request: Request, pk=None, attachment=None):  # noqa: ANN001, ANN201
        return self._file(request, attachment, True)

    # -- worklists and summaries --------------------------------------------------------------
    @extend_schema(responses=ActionSerializer(many=True))
    @action(detail=False, methods=["get"])
    def mine(self, request: Request) -> Response:
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    @extend_schema(responses=ActionSerializer(many=True))
    @action(detail=False, methods=["get"], url_path="verification-queue")
    def verification_queue(self, request: Request) -> Response:
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    @extend_schema(responses=None)
    @action(detail=False, methods=["get"])
    def summary(self, request: Request) -> Response:
        return Response(selectors.summary(self.filter_queryset(selectors.actions_visible_to(request.user))))


_ = TRANSITIONS
