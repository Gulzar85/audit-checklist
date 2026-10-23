"""Workflow URLs (mounted at ``/workflows/``).  Public identifiers are UUIDs."""

from __future__ import annotations

from django.urls import path

from . import views as v

app_name = "workflows"

urlpatterns = [
    path("", v.WorkflowListView.as_view(), name="list"),
    path("new/", v.WorkflowCreateView.as_view(), name="create"),
    path("tasks/", v.TaskListView.as_view(), name="tasks"),
    path("tasks/<uuid:pk>/claim/", v.TaskClaimView.as_view(), name="task_claim"),
    path("tasks/<uuid:pk>/release/", v.TaskReleaseView.as_view(), name="task_release"),
    path("tasks/<uuid:pk>/reassign/", v.TaskReassignView.as_view(), name="task_reassign"),
    path("instances/", v.InstanceListView.as_view(), name="instances"),
    path("instances/<uuid:pk>/", v.InstanceDetailView.as_view(), name="instance"),
    path("instances/<uuid:pk>/transition/", v.InstanceTransitionView.as_view(), name="instance_transition"),
    path("instances/<uuid:pk>/cancel/", v.InstanceCancelView.as_view(), name="instance_cancel"),
    path("start/", v.StartView.as_view(), name="start"),
    path("versions/<uuid:pk>/", v.VersionView.as_view(), name="version"),
    path("versions/<uuid:pk>/settings/", v.VersionSaveView.as_view(), name="version_save"),
    path("versions/<uuid:pk>/state/", v.StateSaveView.as_view(), name="state_save"),
    path("versions/<uuid:pk>/state/delete/", v.StateDeleteView.as_view(), name="state_delete"),
    path("versions/<uuid:pk>/transition/", v.TransitionSaveView.as_view(), name="transition_save"),
    path("versions/<uuid:pk>/transition/delete/", v.TransitionDeleteView.as_view(), name="transition_delete"),
    path("versions/<uuid:pk>/validate/", v.VersionValidateView.as_view(), name="version_validate"),
    path("versions/<uuid:pk>/publish/", v.VersionPublishView.as_view(), name="version_publish"),
    path("versions/<uuid:pk>/clone/", v.VersionCloneView.as_view(), name="version_clone"),
    path("versions/<uuid:pk>/archive/", v.VersionArchiveView.as_view(), name="version_archive"),
    path("<uuid:pk>/", v.WorkflowDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", v.WorkflowEditView.as_view(), name="edit"),
    path("<uuid:pk>/draft/", v.DraftCreateView.as_view(), name="draft"),
    path("<uuid:pk>/archive/", v.DefinitionArchiveView.as_view(), name="archive"),
]
