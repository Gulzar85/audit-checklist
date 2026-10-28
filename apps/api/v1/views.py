"""API v1 views."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import connection
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import APIRootSerializer, HealthSerializer


#: Every collection / entry endpoint of the API (relative to ``/api/v1/``).  Per-object routes (``{id}/...``) are documented in the schema.
ENDPOINTS: dict[str, str] = {
    "health": "health/", "schema": "/api/schema/", "docs": "/api/docs/",
    # identity and access
    "me": "me/", "me_preferences": "me/preferences/", "appearance": "preferences/appearance/", "my_access": "access/me/", "permissions": "permissions/",
    "users": "users/", "roles": "roles/", "members": "members/",
    # organizations, locations, themes
    "organizations": "organizations/", "location_types": "location-types/", "locations": "locations/", "location_tree": "locations/tree/",
    "themes": "themes/", "active_theme": "themes/active/",
    # checklists
    "checklists": "checklists/", "answer_types": "checklists/answer-types/", "rule_metadata": "checklists/rules/metadata/",
    # assignments and executions
    "assignments": "assignments/", "my_assignments": "my-assignments/", "executions": "executions/",
    # offline
    "offline_session": "offline/session/", "offline_manifest": "offline/manifest/", "offline_assignments": "offline/assignments/", "offline_sync": "offline/sync/",
    "offline_resolve_conflict": "offline/resolve-conflict/",
    # evidence
    "evidence_limits": "evidence/limits/", "evidence_uploads": "evidence/uploads/", "attachments": "attachments/",
    # findings
    "findings": "findings/", "findings_summary": "findings/summary/", "finding_categories": "finding-categories/", "finding_severities": "finding-severities/",
    # corrective actions
    "corrective_actions": "corrective-actions/", "my_corrective_actions": "my-corrective-actions/", "corrective_actions_verification_queue": "corrective-actions/verification-queue/",
    "corrective_actions_summary": "corrective-actions/summary/",
    # workflows
    "scoring_metadata": "scoring/metadata/", "evaluation_metadata": "evaluation/metadata/", "notifications": "notifications/", "notification_metadata": "notifications/metadata/", "notification_unread_count": "notifications/unread-count/", "notification_preferences": "notifications/preferences/", "notification_rules": "notifications/rules/", "notification_templates": "notifications/templates/", "notification_deliveries": "notifications/deliveries/", "scheduling_tasks": "scheduling/tasks/", "scheduling_schedules": "scheduling/schedules/", "scheduling_jobs": "scheduling/jobs/", "scheduling_upcoming": "scheduling/upcoming/", "scheduling_diagnostics": "scheduling/diagnostics/", "reports": "reports/", "reports_saved": "reports/saved/", "reports_runs": "reports/runs/", "workflows": "workflows/", "workflow_metadata": "workflows/metadata/", "workflow_instances": "workflow-instances/",
    "workflow_tasks": "workflow-tasks/", "my_workflow_tasks": "my-workflow-tasks/", "workflow_tasks_summary": "workflow-tasks/summary/",
}


class APIRootView(APIView):
    """Discovery endpoint listing the API version and available routes."""

    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []
    serializer_class = APIRootSerializer

    @extend_schema(responses=APIRootSerializer)
    def get(self, request: Request) -> Response:
        return Response({"version": "v1", "endpoints": {name: str(request.build_absolute_uri(path)) for name, path in ENDPOINTS.items()}})


class HealthCheckView(APIView):
    """API health probe (unauthenticated, no throttling)."""

    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []
    throttle_classes: list[Any] = []
    serializer_class = HealthSerializer

    @extend_schema(responses=HealthSerializer)
    def get(self, request: Request) -> Response:
        database_ok = self._database_ok()
        payload = {
            "status": "ok" if database_ok else "degraded",
            "version": getattr(settings, "APP_VERSION", "0.1.0"),
            "environment": getattr(settings, "ENVIRONMENT", "development"),
            "checks": {"database": {"status": "ok" if database_ok else "error"}},
        }
        status_code = 200 if database_ok else 503
        return Response(payload, status=status_code)

    @staticmethod
    def _database_ok() -> bool:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:  # pragma: no cover - infra dependent
            return False
        return True
