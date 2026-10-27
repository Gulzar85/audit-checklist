"""Email providers.

A provider is the single seam between the application and a delivery mechanism
(Django SMTP, Microsoft 365/Graph, ...). The default implementation uses
Django's ``MAILERS`` configuration; the Microsoft 365 provider is a documented
placeholder for a later phase.
"""

from __future__ import annotations

from .base import BaseEmailProvider, EmailMessage, EmailProviderError, SendResult
from .django_smtp import DjangoEmailProvider

__all__ = [
    "BaseEmailProvider",
    "EmailMessage",
    "EmailProviderError",
    "SendResult",
    "DjangoEmailProvider",
]
