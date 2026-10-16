"""Access-control URL configuration (mounted at ``/access/``)."""

from __future__ import annotations

from django.urls import path

from .views import (
    AccessSummaryView,
    MemberAccessView,
    MemberAssignRoleView,
    MemberListView,
    MemberRemoveRoleView,
    MemberStatusView,
    RoleCreateView,
    RoleDetailView,
    RoleListView,
    RoleStatusView,
    RoleUpdateView,
)

app_name = "access"

urlpatterns = [
    path("me/", AccessSummaryView.as_view(), name="my_access"),
    path("roles/", RoleListView.as_view(), name="role_list"),
    path("roles/new/", RoleCreateView.as_view(), name="role_create"),
    path("roles/<uuid:pk>/", RoleDetailView.as_view(), name="role_detail"),
    path("roles/<uuid:pk>/edit/", RoleUpdateView.as_view(), name="role_update"),
    path("roles/<uuid:pk>/status/", RoleStatusView.as_view(), name="role_status"),
    path("members/", MemberListView.as_view(), name="member_list"),
    path("members/<uuid:pk>/", MemberAccessView.as_view(), name="member_access"),
    path("members/<uuid:pk>/assign/", MemberAssignRoleView.as_view(), name="member_assign"),
    path("members/<uuid:pk>/status/", MemberStatusView.as_view(), name="member_status"),
    path("assignments/<uuid:pk>/remove/", MemberRemoveRoleView.as_view(), name="assignment_remove"),
]