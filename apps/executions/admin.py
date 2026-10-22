"""Django admin for executions: a *read-only* inspection tool.

No add / change / delete: a write here would skip the execution services (eligibility, validation, revisions, the assignment
lifecycle, audit) - and the database guard would refuse changes to a submitted execution anyway.  Non-superusers only see
executions inside their organization/location scope.
"""

from __future__ import annotations

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from . import selectors
from .models import ChecklistExecution, ChecklistResponse, SyncReceipt


class _ReadOnly:
    actions = None

    def has_add_permission(self, request, *args, **kwargs) -> bool:  # noqa: ANN001, ANN002, ANN003
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


class ResponseInline(_ReadOnly, admin.TabularInline):
    model = ChecklistResponse
    extra = 0
    fields = ("item", "status", "value", "comment", "revision", "is_hidden", "answered_by", "answered_at")
    readonly_fields = fields
    can_delete = False


@admin.register(ChecklistExecution)
class ChecklistExecutionAdmin(_ReadOnly, SimpleHistoryAdmin):
    list_display = ("reference", "organization", "assignment", "version", "status", "started_by", "started_at", "submitted_at")
    list_filter = ("status", "organization")
    search_fields = ("assignment__location__name", "version__template__name", "version__template__code")
    list_select_related = ("organization", "assignment__location", "version__template", "started_by")
    ordering = ("-started_at",)
    date_hierarchy = "started_at"
    inlines = [ResponseInline]

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(pk__in=selectors.executions_visible_to(request.user).values("pk"))


@admin.register(ChecklistResponse)
class ChecklistResponseAdmin(_ReadOnly, SimpleHistoryAdmin):
    list_display = ("execution", "item", "status", "revision", "is_hidden", "answered_by", "answered_at")
    list_filter = ("status", "is_hidden")
    search_fields = ("execution__assignment__location__name", "item__code", "item__question")
    list_select_related = ("execution", "item", "answered_by")

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(execution__in=selectors.executions_visible_to(request.user).values("pk"))


@admin.register(SyncReceipt)
class SyncReceiptAdmin(_ReadOnly, admin.ModelAdmin):
    """Idempotency receipts of offline synchronization: read-only diagnostics.

    The fingerprint and result are shown (they hold answers' *outcomes*, never credentials); nothing can be added, changed or
    deleted here, so a receipt can never be edited to replay or suppress an operation.  Retention: ``prune_sync_receipts``.
    """

    list_display = ("operation_id", "op_type", "status", "organization", "user", "assignment", "execution", "created_at")
    list_filter = ("status", "op_type", "organization")
    search_fields = ("operation_id", "assignment__location__name")
    list_select_related = ("organization", "user", "assignment", "execution")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(execution__in=selectors.executions_visible_to(request.user).values("pk"))

