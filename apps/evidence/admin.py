"""Django admin for evidence: a read-only inspection tool.

No add / change / delete and no actions: an administrator can *see* lifecycle state, scan status, uploader, hash and attachments, but cannot
mark a file clean, edit its facts or remove history (the database trigger would refuse anyway).  The storage key is deliberately not
shown, and there is no link that serves the file.  Non-superusers only see evidence attached to executions they may see.
"""

from __future__ import annotations

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from apps.executions import selectors as exec_selectors

from .models import EvidenceAsset, EvidenceAttachment


class _ReadOnly:
    actions = None

    def has_add_permission(self, request, *args, **kwargs) -> bool:  # noqa: ANN001, ANN002, ANN003
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


class AttachmentInline(_ReadOnly, admin.TabularInline):
    model = EvidenceAttachment
    extra = 0
    fields = ("target_type", "execution", "item", "caption", "attached_by", "created_at", "detached_at", "detach_reason")
    readonly_fields = fields
    can_delete = False


@admin.register(EvidenceAsset)
class EvidenceAssetAdmin(_ReadOnly, SimpleHistoryAdmin):
    list_display = ("original_name", "organization", "kind", "content_type", "size", "status", "scan_status", "uploaded_by", "created_at")
    list_filter = ("status", "scan_status", "kind", "organization")
    search_fields = ("original_name", "sha256")
    exclude = ("storage_key",)
    list_select_related = ("organization", "uploaded_by")
    date_hierarchy = "created_at"
    inlines = [AttachmentInline]

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        visible = exec_selectors.executions_visible_to(request.user).values("pk")
        return qs.filter(attachments__execution__in=visible).distinct()


@admin.register(EvidenceAttachment)
class EvidenceAttachmentAdmin(_ReadOnly, SimpleHistoryAdmin):
    list_display = ("asset", "organization", "target_type", "execution", "item", "attached_by", "created_at", "detached_at")
    list_filter = ("target_type", "organization")
    list_select_related = ("asset", "organization", "execution", "item", "attached_by")
    date_hierarchy = "created_at"

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(execution__in=exec_selectors.executions_visible_to(request.user).values("pk"))
