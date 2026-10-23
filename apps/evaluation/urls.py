"""Evaluation screens, mounted at ``/evaluation/``."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "evaluation"

urlpatterns = [
    path("versions/<uuid:version>/", views.EditorView.as_view(), name="editor"),
    path("versions/<uuid:version>/settings/", views.SettingsView.as_view(), name="settings"),
    path("versions/<uuid:version>/preview/", views.PreviewView.as_view(), name="preview"),
    path("versions/<uuid:version>/rules/new/", views.RuleFormView.as_view(), name="rule_new"),
    path("versions/<uuid:version>/rules/<str:code>/", views.RuleFormView.as_view(), name="rule"),
    path("versions/<uuid:version>/rules/<str:code>/toggle/", views.RuleToggleView.as_view(), name="rule_toggle"),
    path("versions/<uuid:version>/rules/<str:code>/move/", views.RuleMoveView.as_view(), name="rule_move"),
    path("versions/<uuid:version>/rules/<str:code>/delete/", views.RuleDeleteView.as_view(), name="rule_delete"),
    path("executions/<uuid:pk>/", views.ExecutionEvaluationView.as_view(), name="execution"),
    path("executions/<uuid:pk>/reevaluate/", views.ReevaluateView.as_view(), name="reevaluate"),
]
