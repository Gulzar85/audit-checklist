"""Default email provider backed by Django's ``MAILERS`` configuration.

This is the correct production path for SMTP (including the Microsoft 365 SMTP relay) and the correct path for development (``console`` / ``locmem``).  An SMTP
server's acceptance is *provider acceptance*, never confirmed delivery.  Errors are classified into sanitized categories; the raw exception text is never kept.
"""

from __future__ import annotations

import smtplib
import socket

from django.core import mail

from .base import BaseEmailProvider, EmailMessage, EmailProviderError, SendResult


class DjangoEmailProvider(BaseEmailProvider):
    name = "django"

    def __init__(self, mailer: str = "default") -> None:
        self.mailer = mailer

    def send(self, message: EmailMessage) -> SendResult:
        try:
            email = mail.EmailMultiAlternatives(subject=message.subject, body=message.body, from_email=message.from_email, to=message.to)
            if message.html_body:
                email.attach_alternative(message.html_body, "text/html")
            sent = email.send(using=self.mailer)
        except smtplib.SMTPRecipientsRefused as exc:
            raise EmailProviderError("recipient refused", category="invalid_recipient", transient=False) from exc
        except smtplib.SMTPResponseException as exc:
            transient = 400 <= int(exc.smtp_code) < 500
            raise EmailProviderError("smtp error", category="provider_error" if transient else "rejected", transient=transient, status_code=int(exc.smtp_code)) from exc
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, ConnectionError) as exc:
            raise EmailProviderError("connection error", category="connection", transient=True) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise EmailProviderError("timeout", category="timeout", transient=True) from exc
        except OSError as exc:
            raise EmailProviderError("connection error", category="connection", transient=True) from exc
        except Exception as exc:  # noqa: BLE001 - classified, the raw text is never kept
            raise EmailProviderError("unexpected error", category="unexpected", transient=True) from exc
        if not sent:
            raise EmailProviderError("the backend accepted nothing", category="rejected", transient=False)
        return SendResult(provider=self.name)
