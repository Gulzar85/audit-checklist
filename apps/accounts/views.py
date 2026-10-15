"""Account & identity views (all class-based).

Same contract as the rest of the platform: reads come from selectors, writes go
through services, and authorization asks the policy layer.  No business logic or
authorization rule lives here.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView as DjangoLoginView
from django.contrib.auth.views import LogoutView as DjangoLogoutView
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render, resolve_url
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import FormView, TemplateView, UpdateView

from apps.organizations.selectors import organizations_visible_to
from apps.themes.forms import AppearanceForm

from . import entra, services, throttling
from .constants import (
    ACTIVE_ORGANIZATION_SESSION_KEY,
    OIDC_FLOW_SESSION_KEY,
    POST_LOGIN_REDIRECT_SESSION_KEY,
)
from .context import get_active_organization
from .exceptions import REASON_MESSAGES, IdentityNotAllowed
from .forms import EmailAuthenticationForm, UserProfileUpdateForm
from .policies import can_switch_organization
from .selectors import get_memberships

logger = logging.getLogger("apps.accounts")

THROTTLE_STATUS = 429


def _render_throttled(request: HttpRequest) -> HttpResponse:
    """The safe "too many attempts" page (reuses the authentication error view)."""
    return render(
        request,
        "authentication/authentication_error.html",
        {
            "reason": "rate_limited",
            "message": REASON_MESSAGES["rate_limited"],
        },
        status=THROTTLE_STATUS,
    )



def _safe_redirect_target(request: HttpRequest, candidate: str | None) -> str | None:
    """Return ``candidate`` only if it is a safe internal destination (spec §51)."""
    if not candidate:
        return None
    if url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return None


def _store_next(request: HttpRequest) -> None:
    target = _safe_redirect_target(request, request.GET.get("next"))
    if target:
        request.session[POST_LOGIN_REDIRECT_SESSION_KEY] = target


class CredentialThrottleMixin:
    """Refuse further sign-in attempts once a caller has failed too many times.

    Only the methods that *act* on a credential are guarded (a ``GET`` of the
    sign-in form is never blocked).  Subclasses record failures and clear the
    counter through the ``throttling`` helpers.
    """

    throttle_scope = throttling.PASSWORD_SCOPE
    throttle_methods: frozenset[str] = frozenset({"POST"})

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.method in self.throttle_methods and throttling.is_blocked(
            request, self.throttle_scope
        ):
            services.record_authentication(success=False, reason="rate_limited")
            logger.warning("Sign-in rate limit reached for a client")
            return _render_throttled(request)
        return super().dispatch(request, *args, **kwargs)


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------
class LoginView(CredentialThrottleMixin, DjangoLoginView):
    """The sign-in screen: Microsoft Entra ID first, local password as fallback."""

    template_name = "authentication/login.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True

    def form_invalid(self, form: Any) -> HttpResponse:
        throttling.record_failure(self.request, self.throttle_scope)
        return super().form_invalid(form)

    def form_valid(self, form: Any) -> HttpResponse:
        throttling.clear(self.request, self.throttle_scope)
        return super().form_valid(form)


class MicrosoftLoginView(View):
    """Begin the MSAL authorization-code flow (``GET`` only)."""

    def get(self, request: HttpRequest) -> HttpResponse:
        if not entra.is_configured():
            messages.error(request, "Microsoft sign-in is not configured.")
            return redirect("accounts:login")

        _store_next(request)
        redirect_uri = settings.MICROSOFT_REDIRECT_URI or request.build_absolute_uri(
            reverse("accounts:microsoft_callback")
        )
        try:
            flow = entra.build_client().initiate_auth_code_flow(
                scopes=list(settings.MICROSOFT_SCOPES), redirect_uri=redirect_uri
            )
        except Exception:  # pragma: no cover - network/misconfiguration
            logger.exception("Failed to initiate Microsoft sign-in")
            return self._fail(request, "authentication_failed")

        if not flow or "auth_uri" not in flow:
            logger.error("MSAL did not return an authorization URI")
            return self._fail(request, "authentication_failed")

        # The flow (state + nonce + PKCE verifier) is kept server-side only.
        request.session[OIDC_FLOW_SESSION_KEY] = flow
        return HttpResponseRedirect(flow["auth_uri"])

    def _fail(self, request: HttpRequest, reason: str) -> HttpResponse:
        services.record_authentication(success=False, reason=reason)
        request.session["auth_error_reason"] = reason
        return redirect("accounts:auth_error")


class MicrosoftCallbackView(CredentialThrottleMixin, View):
    """Handle the Entra redirect: validate state, resolve identity, sign in."""

    throttle_scope = throttling.MICROSOFT_SCOPE
    throttle_methods = frozenset({"GET"})

    def get(self, request: HttpRequest) -> HttpResponse:
        flow = request.session.pop(OIDC_FLOW_SESSION_KEY, None)
        if not flow:
            # No in-flight flow (bookmarked URL, expired session, replay).
            services.record_authentication(success=False, reason="invalid_state")
            return self._fail(request, "invalid_state")

        if not entra.is_configured():
            return self._fail(request, "authentication_failed")

        try:
            # MSAL validates state, nonce, PKCE and the signed ID token here.
            result = entra.build_client().acquire_token_by_auth_code_flow(
                flow, request.GET
            )
        except Exception:
            logger.exception("Microsoft callback token acquisition failed")
            return self._fail(request, "authentication_failed")

        claims = (result or {}).get("id_token_claims")
        if not claims:
            return self._fail(request, self._error_code(result))

        identity = entra.claims_to_identity(claims)
        try:
            user = authenticate(request, msal_identity=identity)
        except IdentityNotAllowed as exc:
            services.record_authentication(user=exc.user, success=False, reason=exc.reason)
            return self._fail(request, exc.reason)

        if user is None:
            return self._fail(request, "authentication_failed")

        login(request, user)
        throttling.clear(request, self.throttle_scope)
        services.record_authentication(user=user, success=True)
        logger.info("Microsoft Entra sign-in succeeded for user %s", user.pk)

        target = _safe_redirect_target(
            request, request.session.pop(POST_LOGIN_REDIRECT_SESSION_KEY, None)
        ) or resolve_url(settings.LOGIN_REDIRECT_URL)
        return HttpResponseRedirect(target)

    @staticmethod
    def _error_code(result: Any) -> str:
        code = (result or {}).get("error") if isinstance(result, dict) else None
        if code in {"access_denied", "invalid_request", "invalid_client"}:
            return code
        return "authentication_failed"

    def _fail(self, request: HttpRequest, reason: str) -> HttpResponse:
        throttling.record_failure(request, self.throttle_scope)
        request.session["auth_error_reason"] = reason
        return redirect("accounts:auth_error")


class AuthenticationErrorView(TemplateView):
    """A safe failure page - never leaks provider detail, tokens or stack traces."""

    template_name = "authentication/authentication_error.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        reason = self.request.session.pop("auth_error_reason", "")
        context["reason"] = reason
        context["message"] = REASON_MESSAGES.get(
            reason, "We could not sign you in. Please try again."
        )
        return context


class LogoutView(DjangoLogoutView):
    """Sign out (POST), clearing the session, and log the event."""

    next_page = "accounts:login"

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = request.user if request.user.is_authenticated else None
        if user is not None:
            services.record_logout(user=user)
        response = super().post(request, *args, **kwargs)

        if getattr(settings, "MICROSOFT_FEDERATED_LOGOUT", False) and settings.MICROSOFT_END_SESSION_ENDPOINT:
            post_logout = settings.MICROSOFT_POST_LOGOUT_REDIRECT_URI or request.build_absolute_uri(
                resolve_url(settings.LOGOUT_REDIRECT_URL)
            )
            return HttpResponseRedirect(
                f"{settings.MICROSOFT_END_SESSION_ENDPOINT}?"
                + urlencode({"post_logout_redirect_uri": post_logout})
            )
        return response


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
class ProfileView(LoginRequiredMixin, TemplateView):
    """The signed-in user's profile."""

    template_name = "accounts/profile.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["memberships"] = get_memberships(user)
        context["active_memberships"] = [m for m in context["memberships"] if m.is_active]
        context["auth_provider"] = (
            "Microsoft Entra ID" if user.is_entra_linked else "Local password"
        )
        context["appearance_mode"] = self._appearance_mode(user)
        return context

    @staticmethod
    def _appearance_mode(user: Any) -> str | None:
        from apps.themes.models import UserAppearance

        return UserAppearance.objects.filter(user=user).values_list("mode", flat=True).first()


class ProfileUpdateView(LoginRequiredMixin, UpdateView):
    """Edit the application-managed profile fields (never the IdP-owned ones)."""

    form_class = UserProfileUpdateForm
    template_name = "accounts/profile_edit.html"
    success_url = None

    def get_object(self, queryset: Any = None) -> Any:
        return self.request.user

    def get_success_url(self) -> str:
        return reverse("accounts:profile")

    def form_valid(self, form: Any) -> HttpResponse:
        response = super().form_valid(form)
        services.log_profile_update(actor=self.request.user)
        messages.success(self.request, "Your profile has been updated.")
        return response


class AccountPreferencesView(LoginRequiredMixin, TemplateView):
    """Preferences landing page (appearance + organization overview)."""

    template_name = "account/preferences.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        from apps.themes.models import UserAppearance

        user = self.request.user
        memberships = get_memberships(user)
        context["memberships"] = memberships
        context["has_multiple_organizations"] = len([m for m in memberships if m.is_active]) > 1
        context["appearance_mode"] = (
            UserAppearance.objects.filter(user=user).values_list("mode", flat=True).first()
        )
        return context


class AppearancePreferenceView(LoginRequiredMixin, FormView):
    """Appearance page.

    The form reuses the theme engine's own ``AppearanceForm`` and service, so
    this app never duplicates theme state or resolution (spec §23 / §24).
    """

    template_name = "account/appearance.html"
    form_class = AppearanceForm
    success_url = reverse_lazy("accounts:appearance")

    def get_initial(self) -> dict[str, Any]:
        initial = super().get_initial()
        initial["mode"] = self._stored_mode() or ""
        return initial

    def _stored_mode(self) -> str | None:
        from apps.themes.models import UserAppearance

        return (
            UserAppearance.objects.filter(user=self.request.user)
            .values_list("mode", flat=True)
            .first()
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["appearance_mode"] = self._stored_mode()
        return context

    def form_valid(self, form: Any) -> HttpResponse:
        from apps.themes import services as theme_services
        from apps.themes.constants import (
            APPEARANCE_COOKIE,
            APPEARANCE_COOKIE_MAX_AGE,
            Appearance,
        )

        mode = form.cleaned_data.get("mode") or None
        theme_services.set_user_appearance(self.request.user, mode, actor=self.request.user)
        response = super().form_valid(form)
        if mode in Appearance.values:
            response.set_cookie(
                APPEARANCE_COOKIE,
                mode,
                max_age=APPEARANCE_COOKIE_MAX_AGE,
                samesite="Lax",
                secure=self.request.is_secure(),
                httponly=False,
            )
        else:
            response.delete_cookie(APPEARANCE_COOKIE)
        messages.success(self.request, "Your appearance preference has been saved.")
        return response


# ---------------------------------------------------------------------------
# Organization context / switching
# ---------------------------------------------------------------------------
class OrganizationSelectView(LoginRequiredMixin, TemplateView):
    """Let the user pick which organization to act within."""

    template_name = "organizations/select.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["organizations"] = list(organizations_visible_to(user).order_by("name"))
        context["current_organization"] = get_active_organization(self.request)
        return context


class OrganizationSwitchView(LoginRequiredMixin, View):
    """Switch the active organization (``POST`` only, membership verified)."""

    def post(self, request: HttpRequest) -> HttpResponse:
        organization_id = request.POST.get("organization")

        # Fetch through the *selector*: an organization the user cannot see 404s,
        # so a forged id reveals nothing (spec §20 / §65).
        organization = get_object_or_404(
            organizations_visible_to(request.user), pk=organization_id
        )
        if not can_switch_organization(request.user, organization):
            raise PermissionDenied

        services.set_active_organization(request, organization)
        messages.success(request, f"You are now working in {organization.name}.")

        target = _safe_redirect_target(request, request.POST.get("next")) or reverse(
            "accounts:profile"
        )
        return HttpResponseRedirect(target)
