"""Organization URL configuration."""

from __future__ import annotations

from django.urls import path

from .views import (
    OrganizationCreateView,
    OrganizationDetailView,
    OrganizationListView,
    OrganizationStatusView,
    OrganizationUpdateView,
)

app_name = "organizations"

urlpatterns = [
    path("", OrganizationListView.as_view(), name="list"),
    path("create/", OrganizationCreateView.as_view(), name="create"),
    path("<uuid:pk>/", OrganizationDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", OrganizationUpdateView.as_view(), name="update"),
    path("<uuid:pk>/status/", OrganizationStatusView.as_view(), name="status"),
]
