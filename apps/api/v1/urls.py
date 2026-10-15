"""API v1 URL configuration."""

from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .locations import LocationTypeViewSet, LocationViewSet
from .identity import MeView, UserViewSet
from .organizations import OrganizationViewSet
from .themes import AppearancePreferenceView, ThemeViewSet
from .views import APIRootView, HealthCheckView

app_name = "v1"

# SimpleRouter (not DefaultRouter): the API root is the explicit APIRootView below.
router = SimpleRouter()
router.register("organizations", OrganizationViewSet, basename="organization")
router.register("location-types", LocationTypeViewSet, basename="location-type")
router.register("locations", LocationViewSet, basename="location")
router.register("themes", ThemeViewSet, basename="theme")
router.register("users", UserViewSet, basename="user")

urlpatterns = [
    path("", APIRootView.as_view(), name="root"),
    path("health/", HealthCheckView.as_view(), name="health"),
    path("me/", MeView.as_view(), name="me"),
    path("preferences/appearance/", AppearancePreferenceView.as_view(), name="appearance"),
    path("", include(router.urls)),
]
