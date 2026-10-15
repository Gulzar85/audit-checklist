"""Microsoft Entra ID integration.

**One clear strategy**: the OpenID Connect authorization-code flow implemented
with Microsoft's official ``msal`` library (ADR 0029).  The library builds the
authorization request, validates the returned ID token (signature, issuer,
audience, nonce, expiry - against the tenant's JWKS) and hands us verified
claims.  We never implement OAuth ourselves, never validate JWTs by hand, and
never persist the access/refresh tokens: after the identity is resolved they are
discarded.

This module is deliberately free of business logic.  It knows how to build an
MSAL client, and how to turn verified ID-token claims into an
:class:`ExternalIdentity` value object.  Mapping that value object onto a Django
:class:`~apps.accounts.models.User` is the service layer's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class EntraNotConfigured(ImproperlyConfigured):
    """Raised when a Microsoft sign-in is attempted without configuration."""


@dataclass(frozen=True)
class EntraConfig:
    """The Entra configuration resolved from Django settings."""

    client_id: str
    client_secret: str
    tenant_id: str
    authority: str
    redirect_uri: str
    post_logout_redirect_uri: str
    scopes: tuple[str, ...]
    allowed_tenants: tuple[str, ...]

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.tenant_id)


def get_config() -> EntraConfig:
    """Return the currently configured Entra settings (never hard-coded)."""
    return EntraConfig(
        client_id=getattr(settings, "MICROSOFT_CLIENT_ID", ""),
        client_secret=getattr(settings, "MICROSOFT_CLIENT_SECRET", ""),
        tenant_id=getattr(settings, "MICROSOFT_TENANT_ID", ""),
        authority=getattr(settings, "MICROSOFT_AUTHORITY", ""),
        redirect_uri=getattr(settings, "MICROSOFT_REDIRECT_URI", ""),
        post_logout_redirect_uri=getattr(settings, "MICROSOFT_POST_LOGOUT_REDIRECT_URI", ""),
        scopes=tuple(getattr(settings, "MICROSOFT_SCOPES", ("openid", "profile", "email"))),
        allowed_tenants=tuple(getattr(settings, "MICROSOFT_ALLOWED_TENANTS", ())),
    )


def is_configured() -> bool:
    return get_config().configured


def build_client() -> Any:
    """Return a ready MSAL :class:`ConfidentialClientApplication`.

    Raises :class:`EntraNotConfigured` when the deployment has no Entra
    configuration, so the failure is explicit rather than a confusing 500.
    """
    config = get_config()
    if not config.configured:
        raise EntraNotConfigured("Microsoft Entra ID is not configured.")

    import msal  # imported lazily so the app runs without the dependency present

    return msal.ConfidentialClientApplication(
        client_id=config.client_id,
        client_credential=config.client_secret,
        authority=config.authority,
    )


@dataclass(frozen=True)
class ExternalIdentity:
    """The subset of verified Entra claims this platform actually needs.

    Only claims that are required are retained (spec: do not store every token
    claim).  Anything else is processed during sign-in and discarded with the
    tokens.
    """

    object_id: str
    tenant_id: str
    email: str
    username: str
    display_name: str
    first_name: str
    last_name: str
    job_title: str

    @property
    def identity_key(self) -> str:
        return self.object_id


def _first(claims: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = claims.get(key)
        if value:
            return str(value)
    return ""


def claims_to_identity(claims: dict[str, Any]) -> ExternalIdentity:
    """Build an :class:`ExternalIdentity` from MSAL-verified ID-token claims.

    ``oid`` (the immutable directory object id) is the external identity key;
    email is *derived* but is never trusted as the identity on its own.
    """
    object_id = _first(claims, "oid", "sub")
    email = _first(claims, "email", "preferred_username", "upn").strip()
    username = _first(claims, "preferred_username", "upn", "email").strip()
    return ExternalIdentity(
        object_id=object_id,
        tenant_id=_first(claims, "tid"),
        email=email,
        username=username,
        display_name=_first(claims, "name"),
        first_name=_first(claims, "given_name"),
        last_name=_first(claims, "family_name"),
        job_title=_first(claims, "jobTitle"),
    )


def tenant_is_allowed(tenant_id: str, config: EntraConfig | None = None) -> bool:
    """Whether ``tenant_id`` may sign in.

    With no explicit allow-list, only the configured tenant is permitted (the
    authority is tenant-specific).  An explicit ``MICROSOFT_ALLOWED_TENANTS``
    list is honoured verbatim, which is what enables multi-tenant sign-in.
    """
    config = config or get_config()
    if config.allowed_tenants:
        return tenant_id in config.allowed_tenants
    return bool(config.tenant_id) and tenant_id == config.tenant_id
