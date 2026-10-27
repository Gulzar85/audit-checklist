"""Microsoft Graph email provider (application permissions, client credentials).

Sends with ``POST https://graph.microsoft.com/v1.0/users/{sender}/sendMail``.  It uses the project's existing Microsoft Entra application (``MICROSOFT_TENANT_ID`` /
``MICROSOFT_CLIENT_ID`` / ``MICROSOFT_CLIENT_SECRET``) through MSAL's client-credentials flow, and needs exactly one Graph application permission, **``Mail.Send``**,
with admin consent - ideally restricted to the single sending mailbox by an Exchange *Application Access Policy*.  Nothing is requested at import time and no
credential is stored anywhere but the environment: if any setting is missing the provider raises a ``not_configured`` error and **never reports success**.

Settings (all optional until this provider is selected with ``NOTIFICATION_EMAIL_PROVIDER=apps.notifications.providers.microsoft365.Microsoft365EmailProvider``):
``NOTIFICATION_GRAPH_SENDER`` (the mailbox that sends), ``NOTIFICATION_GRAPH_TIMEOUT`` (seconds, default 15), ``NOTIFICATION_GRAPH_BASE_URL`` (default Graph v1.0).

Graph answers ``202 Accepted`` for ``sendMail`` without a message id: that is *provider acceptance*, reported as ``sent``, never as confirmed delivery.  Tokens, request
bodies and Graph response bodies are never logged or stored; errors are reduced to sanitized categories (429 and 5xx are transient, 400/403 are permanent).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings

from .base import BaseEmailProvider, EmailMessage, EmailProviderError, SendResult

logger = logging.getLogger(__name__)

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
DEFAULT_BASE_URL = "https://graph.microsoft.com/v1.0"


class Microsoft365EmailProvider(BaseEmailProvider):
    name = "microsoft_graph"

    def __init__(self) -> None:
        self._app: Any = None

    # -- configuration -------------------------------------------------------------------------------------------------------
    def configured(self) -> bool:
        return bool(getattr(settings, "MICROSOFT_AUTH_CONFIGURED", False) and getattr(settings, "NOTIFICATION_GRAPH_SENDER", ""))

    def _token(self) -> str:
        if not self.configured():
            raise EmailProviderError("Microsoft Graph email is not configured", category="not_configured", transient=False)
        try:
            if self._app is None:
                import msal

                self._app = msal.ConfidentialClientApplication(settings.MICROSOFT_CLIENT_ID, authority=settings.MICROSOFT_AUTHORITY, client_credential=settings.MICROSOFT_CLIENT_SECRET)
            result = self._app.acquire_token_for_client(scopes=[GRAPH_SCOPE])
        except Exception as exc:  # noqa: BLE001 - classified; the raw text may contain tenant details
            raise EmailProviderError("token request failed", category="connection", transient=True) from exc
        token = (result or {}).get("access_token")
        if not token:
            raise EmailProviderError("the application could not obtain a Graph token", category="unauthorized", transient=False)
        return str(token)

    # -- delivery ------------------------------------------------------------------------------------------------------------
    def _payload(self, message: EmailMessage) -> dict[str, Any]:
        body = {"contentType": "HTML", "content": message.html_body} if message.html_body else {"contentType": "Text", "content": message.body}
        return {"message": {"subject": message.subject, "body": body, "toRecipients": [{"emailAddress": {"address": a}} for a in message.to]}, "saveToSentItems": False}

    def _post(self, url: str, token: str, payload: dict[str, Any]) -> int:
        """The HTTP seam (replaced in tests).  Returns the status code; raises ``urllib.error.HTTPError`` for 4xx/5xx."""
        request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=float(getattr(settings, "NOTIFICATION_GRAPH_TIMEOUT", 15))) as response:  # noqa: S310 - the URL is a fixed https Graph endpoint
            return int(response.status)

    def send(self, message: EmailMessage) -> SendResult:
        token = self._token()
        base = str(getattr(settings, "NOTIFICATION_GRAPH_BASE_URL", "") or DEFAULT_BASE_URL).rstrip("/")
        url = f"{base}/users/{settings.NOTIFICATION_GRAPH_SENDER}/sendMail"
        try:
            status = self._post(url, token, self._payload(message))
        except urllib.error.HTTPError as exc:
            code = int(exc.code)
            if code in (401, 403):
                raise EmailProviderError("graph refused the application", category="unauthorized", transient=False, status_code=code) from None
            if code == 429:
                raise EmailProviderError("graph throttled the request", category="rate_limited", transient=True, status_code=code) from None
            if code >= 500:
                raise EmailProviderError("graph reported a temporary error", category="provider_error", transient=True, status_code=code) from None
            raise EmailProviderError("graph rejected the message", category="rejected", transient=False, status_code=code) from None
        except TimeoutError as exc:
            raise EmailProviderError("graph timed out", category="timeout", transient=True) from exc
        except OSError as exc:  # urllib.error.URLError, connection resets
            raise EmailProviderError("graph could not be reached", category="connection", transient=True) from exc
        if status not in (200, 202):
            raise EmailProviderError("graph returned an unexpected status", category="provider_error", transient=status >= 500, status_code=status)
        logger.info("notification.graph.accepted")  # no addresses, no tokens, no bodies
        return SendResult(provider=self.name, status_code=status)
