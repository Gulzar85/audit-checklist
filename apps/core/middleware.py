"""Project middleware."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils.csp import CSP
from django.utils.deprecation import MiddlewareMixin


def admin_policy(base: dict[str, Any]) -> dict[str, Any]:
    """Return the admin variant of a CSP configuration.

    Starts from the strict site policy and widens exactly two things (ADR 0020):

    * ``script-src`` gains the SHA-256 hashes of Jazzmin's static inline scripts
      (``settings.ADMIN_CSP_SCRIPT_HASHES``) - never ``'unsafe-inline'``;
    * ``style-src-attr`` allows inline ``style=""`` *attributes* only.

    ``<style>`` elements, external CSS, third-party origins and ``eval`` stay blocked.
    """
    policy = {directive: list(values) for directive, values in base.items()}
    scripts = policy.setdefault("script-src", [CSP.SELF])
    scripts.extend(f"'{digest}'" for digest in getattr(settings, "ADMIN_CSP_SCRIPT_HASHES", ()))
    policy["style-src-attr"] = list(getattr(settings, "ADMIN_CSP_STYLE_ATTR", ()))
    return policy


class AdminContentSecurityPolicyMiddleware(MiddlewareMixin):
    """Give ``/admin/`` its own (still strict) Content-Security-Policy.

    Django's :class:`ContentSecurityPolicyMiddleware` honours a policy attached to
    the response, so this runs *inside* it (listed after it) and only decorates
    admin responses.  It respects whichever header the environment uses: development
    runs the policy report-only, production and tests enforce it.
    """

    def process_response(self, request: HttpRequest, response: HttpResponse) -> HttpResponse:
        if not request.path_info.startswith(reverse("admin:index")):
            return response
        if settings.SECURE_CSP and not hasattr(response, "_csp_config"):
            response._csp_config = admin_policy(settings.SECURE_CSP)  # type: ignore[attr-defined]
        if settings.SECURE_CSP_REPORT_ONLY and not hasattr(response, "_csp_ro_config"):
            response._csp_ro_config = admin_policy(settings.SECURE_CSP_REPORT_ONLY)  # type: ignore[attr-defined]
        return response
