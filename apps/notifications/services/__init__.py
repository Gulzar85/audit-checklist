"""Notification services package.

Public surface lives in :mod:`apps.notifications.services.notification_service`.
"""

from __future__ import annotations

from .notification_service import NotificationService, get_email_provider

__all__ = ["NotificationService", "get_email_provider"]
