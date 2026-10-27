"""Scheduling screens, mounted at ``/scheduling/``."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "jobs"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("schedules/", views.ScheduleListView.as_view(), name="schedules"),
    path("schedules/new/", views.ScheduleFormView.as_view(), name="schedule_new"),
    path("schedules/<uuid:pk>/", views.ScheduleDetailView.as_view(), name="schedule"),
    path("schedules/<uuid:pk>/edit/", views.ScheduleFormView.as_view(), name="schedule_edit"),
    path("schedules/<uuid:pk>/<str:action>/", views.ScheduleActionView.as_view(), name="schedule_action"),
    path("jobs/", views.JobListView.as_view(), name="jobs"),
    path("jobs/<uuid:pk>/", views.JobDetailView.as_view(), name="job"),
    path("jobs/<uuid:pk>/<str:action>/", views.JobActionView.as_view(), name="job_action"),
    path("diagnostics/", views.DiagnosticsView.as_view(), name="diagnostics"),
]
