"""Account views (class-based)."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView as DjangoLoginView
from django.contrib.auth.views import LogoutView as DjangoLogoutView
from django.views.generic import TemplateView

from .forms import EmailAuthenticationForm


class LoginView(DjangoLoginView):
    """Email/password fallback login (Entra ID arrives in a later phase)."""

    template_name = "registration/login.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True


class LogoutView(DjangoLogoutView):
    """Log the user out via POST and return to the landing page."""

    next_page = "core:home"


class ProfileView(LoginRequiredMixin, TemplateView):
    """Minimal authenticated profile page proving the CBV/auth wiring."""

    template_name = "accounts/profile.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["memberships"] = self.request.user.organization_memberships.select_related(
            "organization"
        ).order_by("organization__name")
        return context
