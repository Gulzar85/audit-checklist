"""``/api/v1/evidence/`` and ``/api/v1/attachments/`` - the evidence endpoints (Phase 11).

Thin adapters over ``apps.evidence``: every operation resolves its target through the registry (so organization, scope and state are
checked by trusted server code), and every file is served only after re-checking authorization.  Storage keys never appear in a
response; a client cannot name a Django model, an organization or a user.  Offline devices use the same upload endpoint with a stable
``operation_id`` (idempotent per user) - there is no second protocol.
"""

from __future__ import annotations

from urllib.parse import quote

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from django.conf import settings

from apps.api.exceptions import ConflictAPIException, StructuredValidationError
from apps.api.pagination import StandardResultsSetPagination
from apps.api.permissions import IsActiveUser
from apps.core.utils import parse_uuid
from apps.evidence import selectors, services
from apps.evidence.constants import AssetStatus
from apps.evidence.models import EvidenceAsset, EvidenceAttachment
from apps.evidence.targets import TargetNotFound, get_spec
from apps.evidence.validation import IMAGE_TYPES, describe_limits


def _raise(exc: services.EvidenceError) -> None:
    code = exc.evidence_code
    if exc.http_status == 404:
        raise NotFound(" ".join(exc.messages))
    if exc.http_status == 409:
        raise ConflictAPIException(detail={"detail": " ".join(exc.messages), "code": code}) from exc
    raise StructuredValidationError({"file": list(exc.messages)}, extra={"code": code}) from exc


def run(call):  # noqa: ANN001, ANN201
    try:
        return call()
    except services.EvidenceError as exc:
        _raise(exc)
    except DjangoPermissionDenied as exc:
        raise PermissionDenied from exc


class AssetSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(source="original_name", read_only=True)
    kind = serializers.CharField(read_only=True)
    content_type = serializers.CharField(read_only=True)
    size = serializers.IntegerField(read_only=True)
    sha256 = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    scan_status = serializers.CharField(read_only=True)
    width = serializers.IntegerField(read_only=True)
    height = serializers.IntegerField(read_only=True)
    uploaded_at = serializers.DateTimeField(source="created_at", read_only=True)
    available = serializers.BooleanField(source="is_available", read_only=True)


class AttachmentSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    target_type = serializers.CharField(read_only=True)
    execution = serializers.UUIDField(source="execution_id", read_only=True)
    item = serializers.UUIDField(source="item_id", read_only=True)
    caption = serializers.CharField(read_only=True)
    attached_at = serializers.DateTimeField(source="created_at", read_only=True)
    detached_at = serializers.DateTimeField(read_only=True)
    asset = AssetSerializer(read_only=True)


class UploadSerializer(serializers.Serializer):
    """Documents the multipart fields; unknown fields (organization, user, status ...) are rejected, not ignored."""

    target_type = serializers.CharField(default="checklist_response")
    execution_id = serializers.UUIDField(required=False)
    assignment_id = serializers.UUIDField(required=False)
    item_id = serializers.UUIDField()
    file = serializers.FileField()
    caption = serializers.CharField(required=False, allow_blank=True, max_length=200)
    operation_id = serializers.UUIDField(required=False)
    captured_at = serializers.DateTimeField(required=False)
    client_sha256 = serializers.RegexField(r'^[0-9a-fA-F]{64}$', required=False)


ALLOWED_FIELDS = set(UploadSerializer().fields)


class UploadView(APIView):
    """``POST /evidence/uploads/`` - validate, store privately and attach in one request (201; 200 for an idempotent repeat)."""

    permission_classes = [IsActiveUser]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "evidence_upload"
    throttle_classes = [ScopedRateThrottle]

    @extend_schema(request=UploadSerializer, responses=AttachmentSerializer)
    def post(self, request: Request) -> Response:
        if int(request.META.get("CONTENT_LENGTH") or 0) > int(settings.EVIDENCE_MAX_REQUEST_BYTES):
            raise StructuredValidationError({"file": ["The upload is too large."]}, extra={"code": "file_too_large"})
        unknown = sorted(set(request.data) - ALLOWED_FIELDS)
        if unknown:
            raise StructuredValidationError({k: ["This field cannot be set."] for k in unknown}, extra={"code": "invalid_request"})
        payload = UploadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        ref = {"item_id": str(d["item_id"])}
        if d.get("execution_id"):
            ref["execution_id"] = str(d["execution_id"])
        elif d.get("assignment_id"):
            ref["assignment_id"] = str(d["assignment_id"])
        else:
            raise StructuredValidationError({"execution_id": ["Give an execution_id or an assignment_id."]}, extra={"code": "invalid_request"})
        upload = d["file"]
        result = run(lambda: services.upload(
            request.user, target_type=d["target_type"], ref=ref, filename=upload.name, data=upload.read(int(settings.EVIDENCE_MAX_FILE_BYTES) + 1),
            caption=d.get("caption", ""), operation_id=d.get("operation_id"), captured_at=d.get("captured_at"), client_sha256=d.get("client_sha256", "")))
        body = AttachmentSerializer(result.attachment).data
        return Response({**body, "duplicate": result.duplicate}, status=status.HTTP_200_OK if result.duplicate else status.HTTP_201_CREATED)


class AssetView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses=AssetSerializer)
    def get(self, request: Request, pk) -> Response:  # noqa: ANN001
        asset = selectors.get_asset(request.user, pk)
        if asset is None:
            raise NotFound
        return Response(AssetSerializer(asset).data)


class AssetStatusView(APIView):
    """Whether the file is usable yet: ``pending_scan`` (security check pending), ``available`` (verified) or ``quarantined``."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses=None)
    def get(self, request: Request, pk) -> Response:  # noqa: ANN001
        asset = selectors.get_asset(request.user, pk)
        if asset is None:
            raise NotFound
        return Response({"id": str(asset.pk), "status": asset.status, "scan_status": asset.scan_status, "available": asset.status == AssetStatus.AVAILABLE,
                         "retry": asset.status == AssetStatus.PENDING_SCAN})


class AssetFileView(APIView):
    """``GET /evidence/{id}/download/`` and ``.../preview/`` - the bytes, after re-checking authorization and state."""

    permission_classes = [IsActiveUser]
    inline = False

    @extend_schema(responses=None)
    def get(self, request: Request, pk) -> HttpResponse:  # noqa: ANN001
        asset = selectors.get_asset(request.user, pk)
        if asset is None or not selectors.is_servable(asset) or (self.inline and asset.content_type not in IMAGE_TYPES):
            raise NotFound
        data = run(lambda: services.read_bytes(asset))
        services.record_access(request.user, asset, "preview" if self.inline else "download")
        response = HttpResponse(data, content_type=asset.content_type)
        response["Content-Disposition"] = f"{'inline' if self.inline else 'attachment'}; filename*=UTF-8''{quote(asset.original_name)}"
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        response["Cross-Origin-Resource-Policy"] = "same-origin"
        response.accepted_renderer = None  # a raw file response: not wrapped in the JSON envelope
        return response


class AssetPreviewView(AssetFileView):
    inline = True


def _visible_attachments(user, execution_id=None, item_id=None):  # noqa: ANN001, ANN202
    from apps.executions import selectors as exec_selectors

    execution = exec_selectors.get_execution(user, execution_id) if execution_id else None
    if execution is None:
        return EvidenceAttachment.objects.none()
    qs = EvidenceAttachment.objects.filter(execution=execution).select_related("asset", "item").order_by("position", "created_at")
    if item_id:
        qs = qs.filter(item_id=item_id)
    return qs


class AttachmentListView(APIView):
    """``GET /attachments/?execution=<id>[&item=<id>][&include_detached=1]`` (paginated) and ``POST`` to attach an existing, available file."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses=AttachmentSerializer(many=True))
    def get(self, request: Request) -> Response:
        execution_id, item_id = parse_uuid(request.query_params.get("execution")), parse_uuid(request.query_params.get("item"))
        if execution_id is None:
            raise StructuredValidationError({"execution": ["An execution id is required."]}, extra={"code": "invalid_request"})
        qs = _visible_attachments(request.user, execution_id, item_id)
        if request.query_params.get("include_detached") not in ("1", "true"):
            qs = qs.filter(detached_at__isnull=True)
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(AttachmentSerializer(page, many=True).data)

    @extend_schema(request=None, responses=AttachmentSerializer)
    def post(self, request: Request) -> Response:
        data = request.data
        unknown = sorted(set(data) - {"asset_id", "target_type", "execution_id", "item_id", "caption"})
        if unknown:
            raise StructuredValidationError({k: ["This field cannot be set."] for k in unknown}, extra={"code": "invalid_request"})
        asset = selectors.get_asset(request.user, data.get("asset_id"))
        if asset is None:
            raise NotFound
        ref = {"execution_id": data.get("execution_id"), "item_id": data.get("item_id")}
        attachment = run(lambda: services.attach_existing(request.user, asset, target_type=data.get("target_type", "checklist_response"), ref=ref,
                                                          caption=data.get("caption", "")))
        return Response(AttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED)


class AttachmentDetailView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses=AttachmentSerializer)
    def get(self, request: Request, pk) -> Response:  # noqa: ANN001
        attachment = selectors.get_attachment(request.user, pk)
        if attachment is None:
            raise NotFound
        return Response(AttachmentSerializer(attachment).data)

    @extend_schema(responses=None)
    def delete(self, request: Request, pk) -> Response:  # noqa: ANN001
        """Detach (the file and its history are kept).  403 without permission, 409 once the execution is submitted."""
        attachment = selectors.get_attachment(request.user, pk)
        if attachment is None:
            raise NotFound
        run(lambda: services.detach(request.user, attachment, reason=str(request.query_params.get("reason", ""))[:200]))
        return Response(status=status.HTTP_204_NO_CONTENT)


class TargetAttachmentsView(APIView):
    """``GET /targets/{target_type}/{target_id}/attachments/`` - for ``checklist_response`` the target id is the execution (``?item=`` narrows)."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses=AttachmentSerializer(many=True))
    def get(self, request: Request, target_type: str, target_id) -> Response:  # noqa: ANN001
        try:
            get_spec(target_type)
        except TargetNotFound:
            raise NotFound from None
        qs = _visible_attachments(request.user, target_id, parse_uuid(request.query_params.get("item"))).filter(detached_at__isnull=True)
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(AttachmentSerializer(page, many=True).data)


class EvidenceLimitsView(APIView):
    """The configured allow-list and limits (clients show them before uploading; the server enforces them regardless)."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses=None)
    def get(self, request: Request) -> Response:
        return Response({**describe_limits(), "scan_required": bool(settings.EVIDENCE_SCAN_REQUIRED)})


_ = EvidenceAsset  # (imported for type clarity)
