"""Finding URLs (mounted at ``/findings/``).  Public identifiers are UUIDs."""

from __future__ import annotations

from django.urls import path

from . import views as v

app_name = "findings"


def action(name: str):  # noqa: ANN201
    return v.FindingActionView.as_view(action=name)


urlpatterns = [
    path("", v.FindingListView.as_view(), name="list"),
    path("new/", v.FindingCreateView.as_view(), name="create"),
    path("assignee-options/", v.AssigneeOptionsView.as_view(), name="assignee_options"),
    path("settings/", v.FindingSettingsView.as_view(), name="settings"),
    path("settings/categories/new/", v.CategoryFormView.as_view(), name="category_create"),
    path("settings/categories/<uuid:pk>/", v.CategoryFormView.as_view(), name="category_edit"),
    path("settings/severities/new/", v.SeverityFormView.as_view(), name="severity_create"),
    path("settings/severities/<uuid:pk>/", v.SeverityFormView.as_view(), name="severity_edit"),
    path("<uuid:pk>/", v.FindingDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", v.FindingUpdateView.as_view(), name="edit"),
    path("<uuid:pk>/assign/", v.FindingAssignView.as_view(), name="assign"),
    path("<uuid:pk>/evidence/<uuid:attachment>/download/", v.FindingEvidenceFileView.as_view(), name="evidence_download"),
    path("<uuid:pk>/evidence/<uuid:attachment>/preview/", v.FindingEvidencePreviewView.as_view(), name="evidence_preview"),
    *(path(f"<uuid:pk>/{name}/", action(name), name=name) for name in ("acknowledge", "start", "resolve", "close", "reopen", "cancel")),
]
