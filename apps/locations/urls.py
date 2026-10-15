"""Location URL configuration.  Public identifiers are UUIDs, never names."""

from __future__ import annotations

from django.urls import path

from .views import (
    LocationCreateView,
    LocationDetailView,
    LocationListView,
    LocationPickerView,
    LocationRelationsView,
    LocationStatusView,
    LocationTreeView,
    LocationTypeCreateView,
    LocationTypeListView,
    LocationTypeStatusView,
    LocationTypeUpdateView,
    LocationUpdateView,
)

app_name = "locations"

urlpatterns = [
    path("", LocationListView.as_view(), name="list"),
    path("tree/", LocationTreeView.as_view(), name="tree"),
    path("create/", LocationCreateView.as_view(), name="create"),
    # HTMX fragments.
    path("picker/", LocationPickerView.as_view(), name="picker"),
    path("form-relations/", LocationRelationsView.as_view(), name="form_relations"),
    # Location types (configuration for the hierarchy).
    path("types/", LocationTypeListView.as_view(), name="type_list"),
    path("types/create/", LocationTypeCreateView.as_view(), name="type_create"),
    path("types/<uuid:pk>/edit/", LocationTypeUpdateView.as_view(), name="type_update"),
    path("types/<uuid:pk>/status/", LocationTypeStatusView.as_view(), name="type_status"),
    # Locations.
    path("<uuid:pk>/", LocationDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", LocationUpdateView.as_view(), name="update"),
    path("<uuid:pk>/status/", LocationStatusView.as_view(), name="status"),
]
