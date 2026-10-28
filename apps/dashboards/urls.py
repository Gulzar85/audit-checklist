"""Dashboard screens, mounted at ``/dashboards/``."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "dashboards"

urlpatterns = [
    path("", views.IndexView.as_view(), name="index"),
    path("<str:code>/", views.DashboardView.as_view(), name="dashboard"),
    path("<str:code>/customize/", views.CustomizeView.as_view(), name="customize"),
    path("<str:code>/widgets/<str:widget>/", views.WidgetView.as_view(), name="widget"),
]
