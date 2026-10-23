"""API v1 URL configuration."""

from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .assignments import AssignmentViewSet
from .offline import OfflineAssignmentListView, OfflineManifestView, OfflinePackageView, OfflineSessionView, OfflineSyncView, ResolveConflictView
from .evidence import (
    AssetFileView, AssetPreviewView, AssetStatusView, AssetView, AttachmentDetailView, AttachmentListView, EvidenceLimitsView, TargetAttachmentsView, UploadView,
)
from .corrective_actions import ActionViewSet
from .findings import CategoryViewSet, FindingViewSet, SeverityViewSet
from .executions import AssignmentExecutionView, ExecutionViewSet, MyAssignmentsView, StartExecutionView
from .checklists import AnswerTypeDetailView, AnswerTypeListView, ChecklistVersionViewSet, ChecklistViewSet, RuleMetadataView
from .access import MemberViewSet, MyAccessView, PermissionListView, RoleViewSet
from .locations import LocationTypeViewSet, LocationViewSet
from .identity import MePreferencesView, MeView, UserViewSet
from .organizations import OrganizationViewSet
from .themes import AppearancePreferenceView, ThemeViewSet
from .views import APIRootView, HealthCheckView

app_name = "v1"

# SimpleRouter (not DefaultRouter): the API root is the explicit APIRootView below.
router = SimpleRouter()
router.register("organizations", OrganizationViewSet, basename="organization")
router.register("location-types", LocationTypeViewSet, basename="location-type")
router.register("locations", LocationViewSet, basename="location")
router.register("themes", ThemeViewSet, basename="theme")
router.register("users", UserViewSet, basename="user")
router.register("roles", RoleViewSet, basename="role")
router.register("checklists", ChecklistViewSet, basename="checklist")
router.register("checklist-versions", ChecklistVersionViewSet, basename="checklist-version")
router.register("assignments", AssignmentViewSet, basename="assignment")
router.register("executions", ExecutionViewSet, basename="execution")
router.register("members", MemberViewSet, basename="member")
router.register("findings", FindingViewSet, basename="finding")
router.register("corrective-actions", ActionViewSet, basename="corrective-action")
router.register("finding-categories", CategoryViewSet, basename="finding-category")
router.register("finding-severities", SeverityViewSet, basename="finding-severity")

urlpatterns = [
    path("", APIRootView.as_view(), name="root"),
    path("health/", HealthCheckView.as_view(), name="health"),
    path("my-assignments/", MyAssignmentsView.as_view(), name="my-assignments"),
    path("my-corrective-actions/", ActionViewSet.as_view({"get": "mine"}), name="my-corrective-actions"),
    path("evidence/limits/", EvidenceLimitsView.as_view(), name="evidence-limits"),
    path("evidence/uploads/", UploadView.as_view(), name="evidence-upload"),
    path("evidence/<uuid:pk>/", AssetView.as_view(), name="evidence-detail"),
    path("evidence/<uuid:pk>/status/", AssetStatusView.as_view(), name="evidence-status"),
    path("evidence/<uuid:pk>/download/", AssetFileView.as_view(), name="evidence-download"),
    path("evidence/<uuid:pk>/preview/", AssetPreviewView.as_view(), name="evidence-preview"),
    path("attachments/", AttachmentListView.as_view(), name="attachments"),
    path("attachments/<uuid:pk>/", AttachmentDetailView.as_view(), name="attachment"),
    path("targets/<str:target_type>/<uuid:target_id>/attachments/", TargetAttachmentsView.as_view(), name="target-attachments"),
    path("offline/session/", OfflineSessionView.as_view(), name="offline-session"),
    path("offline/manifest/", OfflineManifestView.as_view(), name="offline-manifest"),
    path("offline/assignments/", OfflineAssignmentListView.as_view(), name="offline-assignments"),
    path("offline/assignments/<uuid:pk>/package/", OfflinePackageView.as_view(), name="offline-package"),
    path("offline/sync/", OfflineSyncView.as_view(), name="offline-sync"),
    path("offline/resolve-conflict/", ResolveConflictView.as_view(), name="offline-resolve-conflict"),
    path("assignments/<uuid:pk>/execution/", AssignmentExecutionView.as_view(), name="assignment-execution"),
    path("assignments/<uuid:pk>/start-execution/", StartExecutionView.as_view(), name="assignment-start-execution"),
    path("me/", MeView.as_view(), name="me"),
    # Must precede the router: ``answer-types`` would otherwise be read as a checklist id.
    path("checklists/rules/metadata/", RuleMetadataView.as_view(), name="rule-metadata"),
    path("checklists/answer-types/", AnswerTypeListView.as_view(), name="answer-types"),
    path("checklists/answer-types/<str:code>/", AnswerTypeDetailView.as_view(), name="answer-type"),
    path("permissions/", PermissionListView.as_view(), name="permissions"),
    path("access/me/", MyAccessView.as_view(), name="my-access"),
    path("me/preferences/", MePreferencesView.as_view(), name="me-preferences"),
    path("preferences/appearance/", AppearancePreferenceView.as_view(), name="appearance"),
    path("", include(router.urls)),
]




