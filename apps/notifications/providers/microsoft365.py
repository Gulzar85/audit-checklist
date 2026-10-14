"""Microsoft 365 provider placeholder.

Deliberately unimplemented in Phase 0.  When Microsoft Graph / OAuth client
credentials are added in a later phase, this class becomes the concrete
integration point and no business code needs to change.
"""

from __future__ import annotations

from .base import BaseEmailProvider, EmailMessage, EmailProviderError


class Microsoft365EmailProvider(BaseEmailProvider):
    """Placeholder for the Microsoft 365 / Graph email provider."""

    def send(self, message: EmailMessage) -> None:
        raise EmailProviderError(
            "Microsoft 365 email delivery is not implemented in Phase 0 and will "
            "be added in a later phase."
        )
