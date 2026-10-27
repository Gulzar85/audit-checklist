"""Django admin for notifications: a *read-only* inspection tool.

A write here would skip validation, the safe-template check, the precedence / policy rules, the delivery state machine and the audit trail, or rewrite what a person
was told.  Events, notifications, deliveries and attempts are append-only (database triggers); rules and templates are changed through the application (services,
API, audit).  Non-superusers only see rows of the organizations in which they hold the matching ``notification.*`` permission.  Message bodies and provider details
are not shown in the list pages; the real recipient address is never stored on a delivery.
"""

from __future__ import annotations

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from . import selectors
from .models import DeliveryAttempt, Notification, NotificationDelivery, NotificationEvent, NotificationRule, NotificationTemplate, UserNotificationPreference


class ReadOnlyMixin:
    actions = None

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


@admin.register(NotificationRule)
class NotificationRuleAdmin(ReadOnlyMixin, SimpleHistoryAdmin):
    list_display = ("code", "event_type", "organization", "enabled", "preference_policy", "precedence", "updated_at")
    list_filter = ("enabled", "preference_policy", "event_type", "organization")
    search_fields = ("code", "name")
    list_select_related = ("organization",)

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(pk__in=selectors.rules_visible_to(request.user).values("pk"))


@admin.register(NotificationTemplate)
class NotificationTemplateAdmin(ReadOnlyMixin, SimpleHistoryAdmin):
    list_display = ("code", "version", "status", "event_type", "organization", "updated_at")
    list_filter = ("status", "event_type", "organization")
    search_fields = ("code", "name")
    list_select_related = ("organization",)

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(pk__in=selectors.templates_visible_to(request.user).values("pk"))


@admin.register(NotificationEvent)
class NotificationEventAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("event_type", "organization", "status", "attempts", "occurred_at", "processed_at")
    list_filter = ("status", "event_type", "organization")
    search_fields = ("dedupe_key",)
    list_select_related = ("organization",)
    date_hierarchy = "created_at"

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(organization_id__in=selectors.rules_visible_to(request.user).values("organization_id"))


@admin.register(Notification)
class NotificationAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("event_type", "recipient", "organization", "priority", "is_read", "created_at")
    list_filter = ("event_type", "priority", "is_read", "organization")
    list_select_related = ("organization", "recipient")
    date_hierarchy = "created_at"
    exclude = ("message",)

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(organization_id__in=selectors.rules_visible_to(request.user).values("organization_id"))


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("channel", "status", "organization", "attempt_count", "provider", "created_at")
    list_filter = ("channel", "status", "organization")
    list_select_related = ("organization",)
    date_hierarchy = "created_at"
    exclude = ("body_text", "body_html", "lock_token")

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(pk__in=selectors.deliveries_visible_to(request.user).values("pk"))


@admin.register(DeliveryAttempt)
class DeliveryAttemptAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("delivery", "number", "outcome", "error_category", "duration_ms", "started_at")
    list_filter = ("outcome",)

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(delivery__in=selectors.deliveries_visible_to(request.user).values("pk"))


@admin.register(UserNotificationPreference)
class UserNotificationPreferenceAdmin(ReadOnlyMixin, SimpleHistoryAdmin):
    list_display = ("user", "organization", "category", "in_app_enabled", "email_enabled")
    list_filter = ("category", "organization")

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.none()
