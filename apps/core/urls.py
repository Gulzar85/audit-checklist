"""Core URL configuration."""

from __future__ import annotations

from django.urls import path

from .views import (
    GlobalSearchView,
    HealthCheckView,
    HealthWidgetView,
    HomeView,
    ManifestView,
    OfflineView,
    ServiceWorkerView,
    UIShowcaseView,
)

app_name = "core"

urlpatterns = [
    path("", HomeView.as_view(), name="home"),
    path("health/", HealthCheckView.as_view(), name="health"),
    path("health/widget/", HealthWidgetView.as_view(), name="health-widget"),
    path("manifest.webmanifest", ManifestView.as_view(), name="manifest"),
    path("service-worker.js", ServiceWorkerView.as_view(), name="service-worker"),
    path("offline/", OfflineView.as_view(), name="offline"),
    path("search/", GlobalSearchView.as_view(), name="search"),
    path("ui/", UIShowcaseView.as_view(), name="ui"),
]
