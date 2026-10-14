"""Default email provider backed by Django's MAILERS configuration."""

from __future__ import annotations

from django.core import mail

from .base import BaseEmailProvider, EmailMessage, EmailProviderError


class DjangoEmailProvider(BaseEmailProvider):
    """Send email using Django's native email machinery.

    This is the correct production path for Microsoft 365 SMTP (configure the
    ``default`` mailer with the M365 SMTP relay) and the correct path for
    development (console/memory backends).
    """

    def __init__(self, mailer: str = "default") -> None:
        self.mailer = mailer

    def send(self, message: EmailMessage) -> None:
        try:
            email = mail.EmailMultiAlternatives(
                subject=message.subject,
                body=message.body,
                from_email=message.from_email,
                to=message.to,
            )
            if message.html_body:
                email.attach_alternative(message.html_body, "text/html")
            email.send(using=self.mailer)
        except Exception as exc:  # pragma: no cover - network dependent
            raise EmailProviderError(str(exc)) from exc
