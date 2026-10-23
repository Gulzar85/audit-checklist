"""Corrective action URLs (mounted at ``/corrective-actions/``).  Public identifiers are UUIDs."""

from __future__ import annotations

from django.urls import path

from . import views as v

app_name = "corrective_actions"


def transition(name: str):  # noqa: ANN201
    return v.ActionTransitionView.as_view(transition=name)


urlpatterns = [
    path("", v.ActionListView.as_view(), name="list"),
    path("mine/", v.MyActionsView.as_view(), name="mine"),
    path("queue/", v.VerificationQueueView.as_view(), name="queue"),
    path("new/", v.ActionCreateView.as_view(), name="create"),
    path("assignee-options/", v.AssigneeOptionsView.as_view(), name="assignee_options"),
    path("<uuid:pk>/", v.ActionDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", v.ActionUpdateView.as_view(), name="edit"),
    path("<uuid:pk>/assign/", v.ActionAssignView.as_view(), name="assign"),
    path("<uuid:pk>/progress/", v.ActionProgressView.as_view(), name="progress"),
    path("<uuid:pk>/evidence/", v.ActionEvidenceUploadView.as_view(), name="evidence_upload"),
    path("<uuid:pk>/evidence/<uuid:attachment>/detach/", v.ActionEvidenceDetachView.as_view(), name="evidence_detach"),
    path("<uuid:pk>/evidence/<uuid:attachment>/download/", v.ActionEvidenceFileView.as_view(), name="evidence_download"),
    path("<uuid:pk>/evidence/<uuid:attachment>/preview/", v.ActionEvidencePreviewView.as_view(), name="evidence_preview"),
    *(path(f"<uuid:pk>/{name}/", transition(name), name=name) for name in ("issue", "start", "block", "resume", "submit", "verify", "reject", "close", "cancel", "reopen")),
]
