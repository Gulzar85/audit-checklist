"""Vocabularies of the notifications engine (Phase 17).  Everything a rule, template or delivery may say comes from here - nothing is free text or code."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class Channel(models.TextChoices):
    IN_APP = "in_app", _("In-app")
    EMAIL = "email", _("Email")


class Category(models.TextChoices):
    """The preference categories a user can switch per channel (informational / operational events of one module)."""

    ASSIGNMENTS = "assignments", _("Checklist assignments")
    EXECUTIONS = "executions", _("Checklist executions")
    FINDINGS = "findings", _("Findings")
    CORRECTIVE_ACTIONS = "corrective_actions", _("Corrective actions")
    WORKFLOWS = "workflows", _("Workflows and approvals")
    EVALUATIONS = "evaluations", _("Automated evaluations")
    SYSTEM = "system", _("System and delivery")


class PreferencePolicy(models.TextChoices):
    """Which user preferences may suppress a notification (documented in docs/notifications-preferences-and-policy.md)."""

    OPTIONAL = "optional", _("Optional - the user can switch off in-app and email")
    OPERATIONAL = "operational", _("Operational - always in-app; the user can switch off email")
    MANDATORY = "mandatory", _("Mandatory - the user cannot switch it off")


class Priority(models.TextChoices):
    LOW = "low", _("Low")
    NORMAL = "normal", _("Normal")
    HIGH = "high", _("High")


class EventStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    PROCESSING = "processing", _("Processing")
    PROCESSED = "processed", _("Processed")
    FAILED = "failed", _("Failed")


class DeliveryStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    PROCESSING = "processing", _("Processing")
    SENT = "sent", _("Sent (accepted by the provider)")
    DELIVERED = "delivered", _("Delivered (confirmed by the provider)")
    RETRY_PENDING = "retry_pending", _("Waiting to retry")
    FAILED = "failed", _("Failed")
    CANCELLED = "cancelled", _("Cancelled")
    SUPPRESSED = "suppressed", _("Suppressed by policy")


#: Statuses a dispatcher may pick up; and the ones that never change again without an explicit operator action.
DISPATCHABLE = (DeliveryStatus.PENDING, DeliveryStatus.RETRY_PENDING)
TERMINAL = (DeliveryStatus.DELIVERED, DeliveryStatus.CANCELLED, DeliveryStatus.SUPPRESSED)

#: Allowed delivery transitions (also enforced by a database trigger).  ``failed -> pending`` is the controlled manual retry.
TRANSITIONS = {
    DeliveryStatus.PENDING: (DeliveryStatus.PROCESSING, DeliveryStatus.CANCELLED),
    DeliveryStatus.PROCESSING: (DeliveryStatus.SENT, DeliveryStatus.RETRY_PENDING, DeliveryStatus.FAILED, DeliveryStatus.PENDING),
    DeliveryStatus.RETRY_PENDING: (DeliveryStatus.PROCESSING, DeliveryStatus.CANCELLED, DeliveryStatus.FAILED),
    DeliveryStatus.SENT: (DeliveryStatus.DELIVERED,),
    DeliveryStatus.FAILED: (DeliveryStatus.PENDING, DeliveryStatus.CANCELLED),
    DeliveryStatus.DELIVERED: (),
    DeliveryStatus.CANCELLED: (),
    DeliveryStatus.SUPPRESSED: (),
}


class AttemptOutcome(models.TextChoices):
    ACCEPTED = "accepted", _("Accepted by the provider")
    TRANSIENT = "transient_failure", _("Temporary failure")
    PERMANENT = "permanent_failure", _("Permanent failure")


class ErrorCategory(models.TextChoices):
    """Sanitized failure categories - the only error information stored (never a raw provider message)."""

    TIMEOUT = "timeout", _("The provider timed out")
    CONNECTION = "connection", _("The provider could not be reached")
    RATE_LIMITED = "rate_limited", _("The provider asked to slow down")
    PROVIDER_ERROR = "provider_error", _("The provider reported a temporary error")
    REJECTED = "rejected", _("The provider rejected the message")
    INVALID_RECIPIENT = "invalid_recipient", _("The recipient address is not valid")
    NO_ADDRESS = "no_address", _("The recipient has no usable email address")
    NOT_CONFIGURED = "not_configured", _("The email provider is not configured")
    UNAUTHORIZED = "unauthorized", _("The provider refused the application's credentials")
    UNEXPECTED = "unexpected", _("An unexpected error occurred")


TRANSIENT_CATEGORIES = (ErrorCategory.TIMEOUT, ErrorCategory.CONNECTION, ErrorCategory.RATE_LIMITED, ErrorCategory.PROVIDER_ERROR)


class TemplateStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    ACTIVE = "active", _("Active")
    ARCHIVED = "archived", _("Archived")


#: The only recipient strategies a rule may use (see ``recipients.py``).
class Strategy(models.TextChoices):
    ASSIGNEE = "assignee", _("The assigned user")
    CREATOR = "creator", _("The creator / requester")
    TASK_ASSIGNEES = "task_assignees", _("The workflow task's assignee(s)")
    USER = "user", _("A specific user")
    ROLE = "role", _("Members of a role")
    PERMISSION = "permission", _("Users holding a permission at the location")


MAX_RECIPIENTS = 200
MAX_STRATEGIES = 6
MAX_FILTERS = 5
MAX_ATTEMPTS_DEFAULT = 5
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 3600
LOCK_TIMEOUT_SECONDS = 600
BULK_LIMIT = 200
PURGE_SETTING = "notifications.purge"
