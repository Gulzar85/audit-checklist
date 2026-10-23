"""Django admin for corrective actions: a *read-only* inspection tool.

There is deliberately no add / change / delete and no bulk action: a write here would skip the lifecycle service (transitions, ownership
and separation-of-duties rules, evidence requirements, scope checks, audit).  Progress updates and verification decisions are append-only
history (also enforced by database triggers).  Non-superusers only see actions inside their own organization / location scope.
"""

from __future__ import annotations

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from . import selectors
from .models import CorrectiveAction, CorrectiveActionUpdate, CorrectiveActionVerification


class ReadOnlyMixin:
    actions = None

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


def _scoped(request, qs, *, through: str = ""):  # noqa: ANN001, ANN202
    if request.user.is_superuser:
        return qs
    visible = selectors.actions_visible_to(request.user).values("pk")
    return qs.filter(**{f"{through or 'pk'}__in": visible})


@admin.register(CorrectiveAction)
class CorrectiveActionAdmin(ReadOnlyMixin, SimpleHistoryAdmin):
    list_display = ("reference", "title", "organization", "finding", "status", "priority", "assigned_to", "due_at", "created_at")
    list_filter = ("status", "priority", "organization")
    search_fields = ("title", "finding__title", "location__name")
    list_select_related = ("organization", "finding", "assigned_to")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        return _scoped(request, super().get_queryset(request))


@admin.register(CorrectiveActionUpdate)
class CorrectiveActionUpdateAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("action", "kind", "author", "progress_percent", "created_at")
    list_filter = ("kind",)
    search_fields = ("body", "action__title")
    list_select_related = ("action", "author")

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        return _scoped(request, super().get_queryset(request), through="action")


@admin.register(CorrectiveActionVerification)
class CorrectiveActionVerificationAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("action", "submission_number", "decision", "verifier", "decided_at")
    list_filter = ("decision",)
    search_fields = ("action__title", "reason", "notes")
    list_select_related = ("action", "verifier")

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        return _scoped(request, super().get_queryset(request), through="action")
