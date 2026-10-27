"""Notification services (Phase 17).  Callers use ``from apps.notifications import services``.

The original Phase 0 seam (``NotificationService.send_email``: a direct, one-off email through the configured provider) is kept for compatibility; everything that is *about a business
event* goes through ``publish`` so that rules, recipients, preferences, templates, delivery history and retries apply.
"""

from . import config, delivery, events, inapp, reminders, retention
from .config import (
    activate_template, archive_template, ensure_default_rule, preview, save_rule, save_template, seed_defaults, set_rule_enabled, template_problems,
)
from .delivery import cancel, confirm_delivery, dispatch_batch, resend, retry
from .errors import NotificationConflict, NotificationError
from .events import process_event, process_pending, publish
from .inapp import archive, mark_all, mark_many, mark_read, set_preference
from .notification_service import NotificationService, get_email_provider
from .reminders import emit_due_reminders
from .retention import purge

__all__ = [
    "NotificationConflict", "NotificationError", "NotificationService", "activate_template", "archive", "archive_template", "cancel", "config", "confirm_delivery", "delivery",
    "dispatch_batch", "emit_due_reminders", "ensure_default_rule", "events", "get_email_provider", "inapp", "mark_all", "mark_many", "mark_read", "preview", "process_event",
    "process_pending", "publish", "purge", "reminders", "resend", "retention", "retry", "save_rule", "save_template", "seed_defaults", "set_preference", "set_rule_enabled",
    "template_problems",
]
