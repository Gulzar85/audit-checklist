"""Assignment URLs (mounted at ``/assignments/``).  Public identifiers are UUIDs."""

from __future__ import annotations

from django.urls import path

from . import views as v

app_name = "assignments"

urlpatterns = [
    path("", v.AssignmentListView.as_view(), name="list"),
    path("new/", v.AssignmentCreateView.as_view(), name="create"),
    path("bulk/", v.BulkAssignView.as_view(), name="bulk"),
    path("assignee-options/", v.AssigneeOptionsView.as_view(), name="assignee_options"),
    path("<uuid:pk>/", v.AssignmentDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", v.AssignmentUpdateView.as_view(), name="edit"),
    path("<uuid:pk>/issue/", v.AssignmentIssueView.as_view(), name="issue"),
    path("<uuid:pk>/cancel/", v.AssignmentCancelView.as_view(), name="cancel"),
    path("<uuid:pk>/reopen/", v.AssignmentReopenView.as_view(), name="reopen"),
    path("<uuid:pk>/priority/", v.AssignmentPriorityView.as_view(), name="priority"),
    path("<uuid:pk>/due-date/", v.AssignmentDueDateView.as_view(), name="due_date"),
    path("<uuid:pk>/assignee/", v.AssignmentAssigneeView.as_view(), name="assignee"),
]
