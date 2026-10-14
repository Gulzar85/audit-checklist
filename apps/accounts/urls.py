"""Account URL configuration."""

from __future__ import annotations

from django.urls import path

from .views import LoginView, LogoutView, ProfileView

app_name = "accounts"

urlpatterns = [
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("profile/", ProfileView.as_view(), name="profile"),
]
