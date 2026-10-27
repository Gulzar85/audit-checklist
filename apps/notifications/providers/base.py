"""Provider contracts for email delivery.

A provider is the single seam between the application and a delivery mechanism.  ``send`` returns a :class:`SendResult` when the provider **accepted** the
message (acceptance is not confirmed delivery) and raises :class:`EmailProviderError` - carrying a sanitized ``category`` and whether the failure is
``transient`` - otherwise.  Providers never put secrets, tokens, recipient lists or raw provider responses into errors or logs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class EmailProviderError(RuntimeError):
    """A provider could not accept a message.  ``category`` is one of ``constants.ErrorCategory``; ``transient`` says whether a retry can help."""

    def __init__(self, message: str = "", *, category: str = "unexpected", transient: bool = False, status_code: int | None = None) -> None:
        super().__init__(message)
        self.category, self.transient, self.status_code = category, transient, status_code


@dataclass(frozen=True)
class SendResult:
    """The provider accepted the message.  ``provider_message_id`` is set when the provider returns one; ``confirmed`` only for providers that confirm delivery."""

    provider: str
    provider_message_id: str = ""
    status_code: int | None = None
    confirmed: bool = False


@dataclass(frozen=True)
class EmailMessage:
    """A provider-agnostic email message."""

    subject: str
    body: str
    to: list[str]
    from_email: str | None = None
    html_body: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


class BaseEmailProvider(ABC):
    """Abstract base class every email provider must implement."""

    name = "provider"

    @abstractmethod
    def send(self, message: EmailMessage) -> SendResult | None:
        """Hand ``message`` to the provider; return a :class:`SendResult` or raise :class:`EmailProviderError`."""
        raise NotImplementedError
