"""``/api/v1/offline/`` - the offline execution protocol (Phase 10).

Thin adapters over ``apps.executions.offline``: the package (a read-only rendering of the assignment's pinned version), the
manifest of what the client may do offline, the synchronization endpoint and conflict resolution.  Nothing here evaluates a rule
or validates an answer; the Phase 9 services do, at sync time, for every operation.  There is no endpoint that touches browser
storage - IndexedDB is client-side.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.api.exceptions import ConflictAPIException, StructuredValidationError
from apps.api.permissions import IsActiveUser
from apps.executions import selectors
from apps.executions.offline import compat, package, sync


class OfflineManifestView(APIView):
    """What the offline engine supports and the limits of one sync request (the client checks its own version against it)."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses=None)
    def get(self, request: Request) -> Response:
        return Response(compat.manifest())


class OfflineAssignmentSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    reference = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    priority = serializers.CharField(read_only=True)
    due_at = serializers.DateTimeField(read_only=True)
    checklist = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    offline = serializers.SerializerMethodField()

    def get_checklist(self, obj) -> dict:  # noqa: ANN001
        v = obj.version
        return {"name": v.template.name, "version_id": str(v.pk), "version_number": v.version_number}

    def get_location(self, obj) -> dict:  # noqa: ANN001
        return {"id": str(obj.location_id), "name": obj.location.name}

    def get_offline(self, obj) -> dict:  # noqa: ANN001
        reasons = self.context["compat"].reasons(obj.version)
        return {"capable": not reasons, "reasons": reasons}


class OfflineAssignmentListView(generics.ListAPIView):
    """The assignments the caller may prepare for offline use (the worklist set), with a per-assignment capability verdict."""

    serializer_class = OfflineAssignmentSerializer
    permission_classes = [IsActiveUser]

    def get_queryset(self):  # noqa: ANN201
        return package.offline_assignments(self.request.user)

    def get_serializer_context(self):  # noqa: ANN201
        return {**super().get_serializer_context(), "compat": package.VersionCompat()}


class OfflinePackageView(APIView):
    """``GET .../assignments/{id}/package/`` - the exact definition and metadata to cache; 409 if it cannot run offline."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses=None)
    def get(self, request: Request, pk) -> Response:  # noqa: ANN001
        assignment = selectors.assignment_for_work(request.user, pk)
        if assignment is None:
            raise NotFound
        try:
            body = package.build_package(request.user, assignment)
        except package.Unsupported as exc:
            raise ConflictAPIException(detail={"detail": "This checklist cannot be used offline.", "code": "offline_unsupported", "reasons": exc.reasons}) from exc
        response = Response(body)
        response["Cache-Control"] = "no-store"  # a personal, authenticated payload: never stored by the HTTP cache or the service worker
        return response


class SyncOperationSerializer(serializers.Serializer):
    """Documentation only - operations are validated by ``sync.validate_envelope`` (unknown keys are rejected)."""

    operation_id = serializers.UUIDField()
    type = serializers.ChoiceField(choices=sync.OPS)
    seq = serializers.IntegerField(required=False)
    assignment_id = serializers.UUIDField()
    version_id = serializers.UUIDField()
    item_id = serializers.UUIDField(required=False)
    value = serializers.JSONField(required=False, allow_null=True)
    not_applicable = serializers.BooleanField(required=False)
    comment = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    base_revision = serializers.IntegerField(required=False, min_value=0)


class OfflineSyncView(APIView):
    """``POST /offline/sync/`` - apply a batch of operations (see ``apps/executions/offline/sync.py`` for the contract)."""

    permission_classes = [IsActiveUser]
    throttle_scope = "offline_sync"
    throttle_classes = [ScopedRateThrottle]

    @extend_schema(request=None, responses=None)
    def post(self, request: Request) -> Response:
        body = request.data
        raw_length = int(request.META.get("CONTENT_LENGTH") or 0)
        if raw_length > compat.MAX_BODY_BYTES:
            raise StructuredValidationError({"__all__": ["The request is too large."]}, extra={"code": "payload_too_large"})
        if not isinstance(body, dict) or not isinstance(body.get("operations"), list):
            raise StructuredValidationError({"operations": ["A list of operations is required."]}, extra={"code": "invalid_request"})
        if body.get("schema_version", compat.SCHEMA_VERSION) != compat.SCHEMA_VERSION:
            raise StructuredValidationError({"schema_version": ["Unsupported schema version; update the application."]}, extra={"code": "unsupported_schema"})
        unknown = sorted(set(body) - {"operations", "schema_version", "device_id"})
        if unknown:
            raise StructuredValidationError({k: ["Unknown field."] for k in unknown}, extra={"code": "invalid_request"})
        operations = body["operations"]
        if not operations:
            raise StructuredValidationError({"operations": ["Send at least one operation."]}, extra={"code": "invalid_request"})
        if len(operations) > compat.MAX_OPERATIONS:
            raise StructuredValidationError({"operations": [f"At most {compat.MAX_OPERATIONS} operations per request."]}, extra={"code": "too_many_operations"})
        response = Response(sync.process_batch(request.user, operations))
        response["Cache-Control"] = "no-store"
        return response


class ResolveConflictView(APIView):
    """``POST /offline/resolve-conflict/`` - keep the server's value, or re-send the local one (revalidated); both audited."""

    permission_classes = [IsActiveUser]
    throttle_scope = "offline_sync"
    throttle_classes = [ScopedRateThrottle]

    @extend_schema(request=None, responses=None)
    def post(self, request: Request) -> Response:
        if not isinstance(request.data, dict):
            raise StructuredValidationError({"__all__": ["An object is required."]}, extra={"code": "invalid_request"})
        response = Response(sync.resolve_conflict(request.user, request.data))
        response["Cache-Control"] = "no-store"
        return response


class OfflineSessionView(APIView):
    """``GET /offline/session/`` - who the server says is signed in, and a CSRF token for the sync POSTs.

    The offline engine calls this whenever it is about to synchronize (and when the workspace opens online): identity and the active
    organization are re-asserted by the server every time, never trusted from the device.  The token is kept in memory only.
    """

    permission_classes = [IsActiveUser]

    @extend_schema(responses=None)
    def get(self, request: Request) -> Response:
        from django.middleware.csrf import get_token

        from apps.accounts.context import get_active_organization

        organization = get_active_organization(request._request)
        response = Response({
            "user_id": str(request.user.pk), "organization_id": str(organization.pk) if organization else None,
            "organization_name": organization.name if organization else "", "csrf_token": get_token(request._request),
        })
        response["Cache-Control"] = "no-store"
        return response
