"""URL routes of the execution screens (namespace ``executions``)."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "executions"

urlpatterns = [
    path("", views.MyWorkView.as_view(), name="list"),
    path("history/", views.HistoryView.as_view(), name="history"),
    path("start/<uuid:pk>/", views.StartExecutionView.as_view(), name="start"),
    path("<uuid:pk>/", views.WorkspaceView.as_view(), name="workspace"),
    path("<uuid:pk>/review/", views.ReviewView.as_view(), name="review"),
    path("<uuid:pk>/submit/", views.SubmitView.as_view(), name="submit"),
    path("<uuid:pk>/cancel/", views.CancelExecutionView.as_view(), name="cancel"),
    path("<uuid:pk>/detail/", views.ExecutionDetailView.as_view(), name="detail"),
]
