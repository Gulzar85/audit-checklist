"""Notification service boundary.

Business events must never talk to SMTP directly:

    Business Event -> NotificationService -> EmailProvider -> (M365)

The provider is selected by configuration, so the Microsoft 365 provider can be
dropped in later without touching business code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.utils.module_loading import import_string

from apps.notifications.providers.base import BaseEmailProvider, EmailMessage

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "apps.notifications.providers.django_smtp.DjangoEmailProvider"


def get_email_provider() -> BaseEmailProvider:
    """Return the configured email provider instance."""
    dotted_path = getattr(settings, "NOTIFICATION_EMAIL_PROVIDER", DEFAULT_PROVIDER)
    provider_cls = import_string(dotted_path)
    return provider_cls()


@dataclass
class NotificationService:
    """Coordinates creation and delivery of notifications."""

    provider: BaseEmailProvider = field(default_factory=get_email_provider)

    def send_email(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        html_body: str | None = None,
        from_email: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Send a single email through the configured provider."""
        message = EmailMessage(
            subject=subject,
            body=body,
            to=to,
            from_email=from_email,
            html_body=html_body,
            context=context or {},
        )
        self.provider.send(message)
        logger.info("notification.email.sent", extra={"recipients": len(to)})
