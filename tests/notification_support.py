"""Builders for the Phase 17 tests: the workflow/corrective-action world plus notification administrators, and a scriptable email provider for failure scenarios."""

from __future__ import annotations

from apps.notifications import constants as k
from apps.notifications.providers.base import BaseEmailProvider, EmailMessage, EmailProviderError, SendResult
from apps.permissions import services as rbac
from apps.permissions.registry import P

from workflows_support import build_workflow_world

NOTIFIER = [P.NOTIFICATION_VIEW, P.NOTIFICATION_CONFIGURE, P.NOTIFICATION_DELIVERIES, P.NOTIFICATION_RETRY]


def build_notification_world(org_a, org_b, make_user, assign, make_type):
    w = build_workflow_world(org_a, org_b, make_user, assign, make_type)
    for key, perms in (("notifier", NOTIFIER), ("mandator", [*NOTIFIER, P.NOTIFICATION_MANDATORY]), ("viewer_only", [P.NOTIFICATION_VIEW]), ("no_notif", [P.LOCATION_VIEW])):
        role = rbac.create_role(organization=org_a, name=f"Notification {key}", permissions=perms)
        user = make_user(email=f"nt-{key}@example.com", organization=org_a, role=None)
        assign(user, org_a, role)  # organization-wide
        w[key] = user
    return w


class ScriptedProvider(BaseEmailProvider):
    """Test provider: ``script`` is a list consumed one entry per send - an ``EmailProviderError`` is raised, anything else is accepted.  ``sent`` records accepted messages."""

    name = "scripted"
    script: list = []
    sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> SendResult:
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
        self.sent.append(message)
        return SendResult(provider=self.name, provider_message_id=f"msg-{len(self.sent)}", status_code=202)

    @classmethod
    def reset(cls, *steps: Exception) -> None:
        cls.script, cls.sent = list(steps), []


def transient(category: str = "provider_error", status: int | None = 503) -> EmailProviderError:
    return EmailProviderError("temporary", category=category, transient=True, status_code=status)


def permanent(category: str = "rejected", status: int | None = 400) -> EmailProviderError:
    return EmailProviderError("rejected", category=category, transient=False, status_code=status)


SCRIPTED = "notification_support.ScriptedProvider"
__all__ = ["SCRIPTED", "ScriptedProvider", "build_notification_world", "k", "permanent", "transient"]
