"""Report screens, mounted at ``/reports/``."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "reporting"

urlpatterns = [
    path("", views.CatalogueView.as_view(), name="catalogue"),
    path("saved/", views.SavedListView.as_view(), name="saved"),
    path("saved/<uuid:pk>/<str:action>/", views.SavedActionView.as_view(), name="saved_action"),
    path("runs/", views.RunListView.as_view(), name="runs"),
    path("runs/<uuid:pk>/", views.RunDetailView.as_view(), name="run"),
    path("runs/<uuid:pk>/download/", views.DownloadView.as_view(), name="download"),
    path("<str:code>/", views.ReportView.as_view(), name="report"),
    path("<str:code>/export/", views.ExportView.as_view(), name="export"),
    path("<str:code>/save/", views.SaveView.as_view(), name="save"),
]
