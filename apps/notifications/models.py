"""Notification domain models (Phase 17).

The flow, kept deliberately separate (ADR 0079):

``NotificationEvent`` (a business event was published; the outbox) -> ``NotificationRule`` matches -> ``Notification`` (one per recipient: the in-app
record and the identity of what the user is told) -> ``NotificationDelivery`` (one per channel: its own state machine) -> ``DeliveryAttempt`` (every try).

*Event created*, *notification created*, *provider accepted* (``sent``) and *confirmed delivered* are four different facts and four different fields.
Rows that explain what happened (events, notifications, deliveries, attempts) are never updated beyond their documented state fields and are only
removed by the retention command (database triggers, ``guards.py``).  Users are deactivated, not deleted, so recipients are ``PROTECT``.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel, UUIDModel

from . import constants as k

User = settings.AUTH_USER_MODEL


class NotificationEvent(UUIDModel):
    """The outbox: a validated business event.  ``dedupe_key`` makes publication idempotent per organization."""

    created_at = models.DateTimeField(auto_now_add=True)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="notification_events")
    event_type = models.CharField(max_length=60)
    source_type = models.CharField(max_length=40)
    source_id = models.UUIDField()
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    occurred_at = models.DateTimeField()
    dedupe_key = models.CharField(max_length=200)
    #: Minimal, validated facts (short strings / numbers only) - never answers, evidence or free text of users.
    metadata = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=k.EventStatus.choices, default=k.EventStatus.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    processed_at = models.DateTimeField(null=True, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "dedupe_key"], name="notification_event_unique_key"),
            models.CheckConstraint(condition=models.Q(status__in=k.EventStatus.values), name="notification_event_status_valid"),
        ]
        indexes = [models.Index(fields=["status", "created_at"], name="notif_event_status_idx"),
                   models.Index(fields=["organization", "event_type", "-created_at"], name="notif_event_type_idx")]

    def __str__(self) -> str:
        return f"{self.event_type} {self.source_id}"


class NotificationTemplate(BaseModel):
    """A safe text template set (subject, plain / HTML email body, in-app title and message).  Only ``{{ allowed_variable }}`` is rendered (``templating.py``)."""

    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, null=True, blank=True, related_name="notification_templates",
                                     help_text="Empty for a system template available to every organization.")
    code = models.SlugField(max_length=60)
    name = models.CharField(max_length=120)
    event_type = models.CharField(max_length=60, help_text="The event whose variables this template may use.")
    locale = models.CharField(max_length=10, default="en")
    status = models.CharField(max_length=10, choices=k.TemplateStatus.choices, default=k.TemplateStatus.DRAFT)
    version = models.PositiveIntegerField(default=1)
    subject = models.CharField(max_length=200, blank=True)
    body_text = models.TextField(blank=True, max_length=5000)
    body_html = models.TextField(blank=True, max_length=10000)
    in_app_title = models.CharField(max_length=160, blank=True)
    in_app_message = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "code", "version"], name="notification_template_unique_version"),
            models.UniqueConstraint(fields=["organization", "code"], condition=models.Q(status="active"), name="notification_template_one_active"),
            models.CheckConstraint(condition=models.Q(status__in=k.TemplateStatus.values), name="notification_template_status_valid"),
        ]
        indexes = [models.Index(fields=["organization", "event_type", "status"], name="notif_tpl_lookup_idx")]

    def __str__(self) -> str:
        return f"{self.code} v{self.version}"


class NotificationRule(BaseModel):
    """Turns one registered event type into notifications: who (strategies), how (channels), what (template), under which policy."""

    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="notification_rules")
    code = models.SlugField(max_length=60)
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=500, blank=True)
    event_type = models.CharField(max_length=60)
    enabled = models.BooleanField(default=True, help_text="A disabled rule is a draft: it is never applied.")
    is_system_default = models.BooleanField(default=False, help_text="Created from the built-in defaults; may be edited or disabled, never deleted.")
    #: Lower number = higher precedence when several rules produce the same recipient and channel (see docs).
    precedence = models.PositiveIntegerField(default=100)
    recipient_strategies = models.JSONField(default=list)
    channels = models.JSONField(default=list)
    #: A template *code*; the active version of the organization's (or the system's) template with that code is used.  Empty = the built-in default.
    template_code = models.SlugField(max_length=60, blank=True)
    priority = models.CharField(max_length=8, choices=k.Priority.choices, default=k.Priority.NORMAL)
    preference_policy = models.CharField(max_length=12, choices=k.PreferencePolicy.choices, default=k.PreferencePolicy.OPTIONAL)
    #: ``[{"attribute", "operator", "value"}]`` over the event's registered facts (Phase 6 operators; ``filters.py``); all must be TRUE.
    filters = models.JSONField(default=list, blank=True)
    exclude_actor = models.BooleanField(default=True, help_text="Do not notify the person who caused the event.")
    revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["precedence", "code"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="notification_rule_unique_code"),
            models.CheckConstraint(condition=models.Q(preference_policy__in=k.PreferencePolicy.values), name="notification_rule_policy_valid"),
            models.CheckConstraint(condition=models.Q(priority__in=k.Priority.values), name="notification_rule_priority_valid"),
        ]
        indexes = [models.Index(fields=["organization", "event_type", "enabled"], name="notif_rule_lookup_idx")]

    def __str__(self) -> str:
        return f"{self.code} ({self.event_type})"


class UserNotificationPreference(BaseModel):
    """A user's choice per category and channel inside one organization.  No row = enabled."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notification_preferences")
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="+")
    category = models.CharField(max_length=24, choices=k.Category.choices)
    in_app_enabled = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=True)
    history = HistoricalRecords()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "organization", "category"], name="notification_preference_unique"),
            models.CheckConstraint(condition=models.Q(category__in=k.Category.values), name="notification_preference_category_valid"),
        ]


class Notification(UUIDModel):
    """What one recipient is told.  The in-app notification *is* this row; the destination is a registered object reference, never a stored URL."""

    created_at = models.DateTimeField(auto_now_add=True)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="notifications")
    recipient = models.ForeignKey(User, on_delete=models.PROTECT, related_name="notifications")
    event = models.ForeignKey(NotificationEvent, on_delete=models.PROTECT, related_name="notifications")
    rule = models.ForeignKey(NotificationRule, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    rule_code = models.CharField(max_length=60)
    rule_revision = models.PositiveIntegerField(default=1)
    template_code = models.CharField(max_length=60, blank=True)
    template_version = models.PositiveIntegerField(default=0)  # 0 = the built-in default
    event_type = models.CharField(max_length=60)
    category = models.CharField(max_length=24, choices=k.Category.choices)
    priority = models.CharField(max_length=8, choices=k.Priority.choices, default=k.Priority.NORMAL)
    preference_policy = models.CharField(max_length=12, choices=k.PreferencePolicy.choices, default=k.PreferencePolicy.OPTIONAL)
    title = models.CharField(max_length=160)
    message = models.CharField(max_length=500, blank=True)
    #: The business object this is about (an allowlisted type); opening it always re-checks the viewer's own permission.
    target_type = models.CharField(max_length=40)
    target_id = models.UUIDField()
    in_app_visible = models.BooleanField(default=True, help_text="False when the recipient's preference suppressed the in-app copy (kept for the delivery history).")
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    #: sha256(event, rule code + revision, recipient): the same event never notifies the same person twice through the same rule.
    idempotency_key = models.CharField(max_length=64)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["idempotency_key"], name="notification_unique_key"),
            models.CheckConstraint(condition=models.Q(is_read=False, read_at__isnull=True) | models.Q(is_read=True, read_at__isnull=False), name="notification_read_consistent"),
            models.CheckConstraint(condition=models.Q(category__in=k.Category.values), name="notification_category_valid"),
        ]
        indexes = [
            models.Index(fields=["recipient", "is_read", "-created_at"], name="notif_inbox_idx"),
            models.Index(fields=["organization", "event_type", "-created_at"], name="notif_org_event_idx"),
            models.Index(fields=["target_type", "target_id"], name="notif_target_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.title} -> {self.recipient_id}"


class NotificationDelivery(UUIDModel):
    """One channel of one notification, with its own state machine (``constants.TRANSITIONS``)."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="notification_deliveries")
    notification = models.ForeignKey(Notification, on_delete=models.PROTECT, related_name="deliveries")
    channel = models.CharField(max_length=10, choices=k.Channel.choices)
    status = models.CharField(max_length=14, choices=k.DeliveryStatus.choices, default=k.DeliveryStatus.PENDING)
    provider = models.CharField(max_length=40, blank=True)
    provider_message_id = models.CharField(max_length=200, blank=True)
    to_masked = models.CharField(max_length=120, blank=True, help_text="The recipient address with the local part masked; the real address is read from the user at send time.")
    subject = models.CharField(max_length=300, blank=True)
    body_text = models.TextField(blank=True)
    body_html = models.TextField(blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=k.MAX_ATTEMPTS_DEFAULT)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)  # the provider accepted the message (NOT confirmed delivery)
    delivered_at = models.DateTimeField(null=True, blank=True)  # only when the provider confirms delivery
    last_error_category = models.CharField(max_length=24, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    suppressed_reason = models.CharField(max_length=60, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    lock_token = models.CharField(max_length=40, blank=True)
    #: ``<notification>|<channel>|<resend sequence>`` - a deliberate resend gets the next sequence and records its reason.
    idempotency_key = models.CharField(max_length=80)
    resend_of = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="resends")
    resend_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["idempotency_key"], name="notification_delivery_unique_key"),
            models.CheckConstraint(condition=models.Q(status__in=k.DeliveryStatus.values), name="notification_delivery_status_valid"),
            models.CheckConstraint(condition=models.Q(attempt_count__gte=0), name="notification_delivery_attempts_valid"),
            models.CheckConstraint(condition=~models.Q(status="retry_pending") | models.Q(next_attempt_at__isnull=False), name="notification_delivery_retry_has_time"),
            models.CheckConstraint(condition=~models.Q(status__in=["sent", "delivered"]) | models.Q(accepted_at__isnull=False), name="notification_delivery_sent_has_acceptance"),
            models.CheckConstraint(condition=~models.Q(status="delivered") | models.Q(delivered_at__isnull=False), name="notification_delivery_delivered_has_time"),
        ]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="notif_delivery_dispatch_idx"),
            models.Index(fields=["organization", "channel", "status", "-created_at"], name="notif_delivery_org_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.channel} {self.status}"


class DeliveryAttempt(UUIDModel):
    """One try to deliver: append-only."""

    created_at = models.DateTimeField(auto_now_add=True)
    delivery = models.ForeignKey(NotificationDelivery, on_delete=models.PROTECT, related_name="attempts")
    number = models.PositiveSmallIntegerField()
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()
    outcome = models.CharField(max_length=20, choices=k.AttemptOutcome.choices)
    error_category = models.CharField(max_length=24, blank=True)
    provider = models.CharField(max_length=40, blank=True)
    provider_message_id = models.CharField(max_length=200, blank=True)
    provider_status = models.PositiveSmallIntegerField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["delivery_id", "number"]
        constraints = [
            models.UniqueConstraint(fields=["delivery", "number"], name="delivery_attempt_unique_number"),
            models.CheckConstraint(condition=models.Q(outcome__in=k.AttemptOutcome.values), name="delivery_attempt_outcome_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.delivery_id}#{self.number} {self.outcome}"
