"""Notification screens, mounted at ``/notifications/``."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.InboxView.as_view(), name="inbox"),
    path("read-all/", views.MarkAllView.as_view(), name="read_all"),
    path("preferences/", views.PreferencesView.as_view(), name="preferences"),
    path("rules/", views.RuleListView.as_view(), name="rules"),
    path("rules/<uuid:pk>/toggle/", views.RuleToggleView.as_view(), name="rule_toggle"),
    path("deliveries/", views.DeliveryListView.as_view(), name="deliveries"),
    path("deliveries/<uuid:pk>/", views.DeliveryDetailView.as_view(), name="delivery"),
    path("deliveries/<uuid:pk>/<str:action>/", views.DeliveryActionView.as_view(), name="delivery_action"),
    path("<uuid:pk>/open/", views.OpenView.as_view(), name="open"),
    path("<uuid:pk>/read/", views.MarkReadView.as_view(), name="read"),
    path("<uuid:pk>/archive/", views.ArchiveView.as_view(), name="archive"),
]
