"""Scoring screens, mounted at ``/scoring/``."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "scoring"

urlpatterns = [
    path("versions/<uuid:version>/", views.EditorView.as_view(), name="editor"),
    path("versions/<uuid:version>/settings/", views.SettingsView.as_view(), name="settings"),
    path("versions/<uuid:version>/weights/", views.WeightsView.as_view(), name="weights"),
    path("versions/<uuid:version>/bands/", views.BandsView.as_view(), name="bands"),
    path("versions/<uuid:version>/critical/", views.CriticalView.as_view(), name="critical"),
    path("versions/<uuid:version>/preview/", views.PreviewView.as_view(), name="preview"),
    path("versions/<uuid:version>/items/<str:code>/", views.ItemRuleView.as_view(), name="item"),
    path("executions/<uuid:pk>/", views.ExecutionScoreView.as_view(), name="execution"),
    path("executions/<uuid:pk>/recalculate/", views.RecalculateView.as_view(), name="recalculate"),
]
