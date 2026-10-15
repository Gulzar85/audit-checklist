"""Account & identity URL configuration (mounted at ``/auth/``)."""

from __future__ import annotations

from django.urls import path

from .views import (
    AccountPreferencesView,
    AppearancePreferenceView,
    AuthenticationErrorView,
    LoginView,
    LogoutView,
    MicrosoftCallbackView,
    MicrosoftLoginView,
    ProfileUpdateView,
    ProfileView,
)

app_name = "accounts"

urlpatterns = [
    # Sign-in / sign-out.
    path("login/", LoginView.as_view(), name="login"),
    path("microsoft/login/", MicrosoftLoginView.as_view(), name="microsoft_login"),
    path("microsoft/callback/", MicrosoftCallbackView.as_view(), name="microsoft_callback"),
    path("error/", AuthenticationErrorView.as_view(), name="auth_error"),
    path("logout/", LogoutView.as_view(), name="logout"),
    # Account.
    path("profile/", ProfileView.as_view(), name="profile"),
    path("profile/edit/", ProfileUpdateView.as_view(), name="profile_edit"),
    path("preferences/", AccountPreferencesView.as_view(), name="preferences"),
    path("appearance/", AppearancePreferenceView.as_view(), name="appearance"),
]
