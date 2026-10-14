"""API v1 URL configuration."""

from __future__ import annotations

from django.urls import path

from .views import APIRootView, HealthCheckView

app_name = "v1"

urlpatterns = [
    path("", APIRootView.as_view(), name="root"),
    path("health/", HealthCheckView.as_view(), name="health"),
]
