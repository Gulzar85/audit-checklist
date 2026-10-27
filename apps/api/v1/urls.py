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
from .evaluation import (
    EvaluationRunDetailView, ExecutionEvaluateView, ExecutionEvaluationDetailView, ExecutionEvaluationRunsView, ExecutionEvaluationView, VersionEvaluationPreviewView,
    VersionEvaluationValidateView, VersionEvaluationView,
)
from .evaluation import MetadataView as EvaluationMetadataView
from . import notifications as nt
from .scoring import (
    ExecutionScoreBreakdownView, ExecutionScoreHistoryView, ExecutionScoreRecalculateView, ExecutionScoreView, VersionScoringPreviewView, VersionScoringValidateView,
    VersionScoringView,
)
from .scoring import MetadataView as ScoringMetadataView
from .workflows import MetadataView, WorkflowInstanceViewSet, WorkflowTaskViewSet, WorkflowVersionViewSet, WorkflowViewSet
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
router.register("workflow-versions", WorkflowVersionViewSet, basename="workflow-version")
router.register("workflow-instances", WorkflowInstanceViewSet, basename="workflow-instance")
router.register("workflow-tasks", WorkflowTaskViewSet, basename="workflow-task")
router.register("workflows", WorkflowViewSet, basename="workflow")
router.register("finding-categories", CategoryViewSet, basename="finding-category")
router.register("finding-severities", SeverityViewSet, basename="finding-severity")

urlpatterns = [
    path("", APIRootView.as_view(), name="root"),
    path("health/", HealthCheckView.as_view(), name="health"),
    path("my-assignments/", MyAssignmentsView.as_view(), name="my-assignments"),
    path("workflows/metadata/", MetadataView.as_view(), name="workflow-metadata"),
    path("scoring/metadata/", ScoringMetadataView.as_view(), name="scoring-metadata"),
    path("evaluation/metadata/", EvaluationMetadataView.as_view(), name="evaluation-metadata"),
    path("checklist-versions/<uuid:pk>/evaluation/", VersionEvaluationView.as_view(), name="version-evaluation"),
    path("checklist-versions/<uuid:pk>/evaluation/validate/", VersionEvaluationValidateView.as_view(), name="version-evaluation-validate"),
    path("checklist-versions/<uuid:pk>/evaluation/preview/", VersionEvaluationPreviewView.as_view(), name="version-evaluation-preview"),
    path("executions/<uuid:pk>/evaluation/", ExecutionEvaluationView.as_view(), name="execution-evaluation"),
    path("executions/<uuid:pk>/evaluation/detail/", ExecutionEvaluationDetailView.as_view(), name="execution-evaluation-detail"),
    path("executions/<uuid:pk>/evaluation/runs/", ExecutionEvaluationRunsView.as_view(), name="execution-evaluation-runs"),
    path("executions/<uuid:pk>/evaluation/evaluate/", ExecutionEvaluateView.as_view(), name="execution-evaluation-evaluate"),
    path("evaluation-runs/<uuid:pk>/", EvaluationRunDetailView.as_view(), name="evaluation-run"),
    path("notifications/", nt.NotificationListView.as_view(), name="notifications"),
    path("notifications/metadata/", nt.MetadataView.as_view(), name="notification-metadata"),
    path("notifications/unread-count/", nt.UnreadCountView.as_view(), name="notification-unread-count"),
    path("notifications/mark-read/", nt.BulkReadView.as_view(), name="notification-mark-read"),
    path("notifications/preferences/", nt.PreferencesView.as_view(), name="notification-preferences"),
    path("notifications/rules/", nt.RuleListView.as_view(), name="notification-rules"),
    path("notifications/rules/<uuid:pk>/", nt.RuleDetailView.as_view(), name="notification-rule"),
    path("notifications/rules/<uuid:pk>/<str:action>/", nt.RuleToggleView.as_view(), name="notification-rule-toggle"),
    path("notifications/templates/", nt.TemplateListView.as_view(), name="notification-templates"),
    path("notifications/templates/preview/", nt.TemplatePreviewView.as_view(), name="notification-template-preview"),
    path("notifications/templates/<uuid:pk>/<str:action>/", nt.TemplateActionView.as_view(), name="notification-template-action"),
    path("notifications/deliveries/", nt.DeliveryListView.as_view(), name="notification-deliveries"),
    path("notifications/deliveries/<uuid:pk>/", nt.DeliveryDetailView.as_view(), name="notification-delivery"),
    path("notifications/deliveries/<uuid:pk>/<str:action>/", nt.DeliveryActionView.as_view(), name="notification-delivery-action"),
    path("notifications/<uuid:pk>/", nt.NotificationDetailView.as_view(), name="notification"),
    path("notifications/<uuid:pk>/read/", nt.MarkReadView.as_view(), name="notification-read"),
    path("notifications/<uuid:pk>/archive/", nt.ArchiveView.as_view(), name="notification-archive"),
    path("checklist-versions/<uuid:pk>/scoring/", VersionScoringView.as_view(), name="version-scoring"),
    path("checklist-versions/<uuid:pk>/scoring/validate/", VersionScoringValidateView.as_view(), name="version-scoring-validate"),
    path("checklist-versions/<uuid:pk>/scoring/preview/", VersionScoringPreviewView.as_view(), name="version-scoring-preview"),
    path("executions/<uuid:pk>/score/", ExecutionScoreView.as_view(), name="execution-score"),
    path("executions/<uuid:pk>/score/breakdown/", ExecutionScoreBreakdownView.as_view(), name="execution-score-breakdown"),
    path("executions/<uuid:pk>/score/history/", ExecutionScoreHistoryView.as_view(), name="execution-score-history"),
    path("executions/<uuid:pk>/score/recalculate/", ExecutionScoreRecalculateView.as_view(), name="execution-score-recalculate"),
    path("my-workflow-tasks/", WorkflowTaskViewSet.as_view({"get": "mine"}), name="my-workflow-tasks"),
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




