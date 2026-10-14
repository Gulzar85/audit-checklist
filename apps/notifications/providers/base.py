"""Provider contracts for email delivery."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class EmailProviderError(RuntimeError):
    """Raised when a provider fails to deliver a message."""


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

    @abstractmethod
    def send(self, message: EmailMessage) -> None:
        """Deliver ``message`` or raise :class:`EmailProviderError`."""
        raise NotImplementedError
