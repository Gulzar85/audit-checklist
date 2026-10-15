"""Class-based views for the organization domain.

Every page is a CBV.  Reads are scoped through the selector layer (an
organization the user cannot see is a 404), authorization goes through the policy
layer (visible-but-forbidden is a 403) and every write goes through the service
layer - so the same rules apply here, in the API and in the admin.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView
from django.views.generic.detail import SingleObjectMixin
from django.views.generic.edit import CreateView, UpdateView
from django_filters.views import FilterView
from django_tables2 import SingleTableMixin

from apps.core.forms import add_validation_error
from apps.core.mixins import PolicyMixin
from apps.core.selectors import recent_activity

from . import services
from .filters import OrganizationFilter
from .forms import OrganizationBrandingForm, OrganizationForm, OrganizationStatusForm
from .models import Organization
from .policies import (
    can_change_organization_status,
    can_create_organization,
    can_manage_organization,
)
from .selectors import organizations_visible_to
from .tables import OrganizationTable


class OrganizationScopedMixin:
    """Fetch organizations only from those the current user may see."""

    def get_queryset(self):  # noqa: ANN201
        return organizations_visible_to(self.request.user)


class OrganizationListView(LoginRequiredMixin, SingleTableMixin, FilterView):
    """List the organizations visible to the current user."""

    table_class = OrganizationTable
    filterset_class = OrganizationFilter
    template_name = "organizations/organization_list.html"
    context_object_name = "organizations"
    table_pagination = {"per_page": 20}

    def get_queryset(self):  # noqa: ANN201
        return organizations_visible_to(self.request.user)

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        context = super().get_context_data(**kwargs)
        context["can_create"] = can_create_organization(self.request.user)
        context["hidden_filters"] = ("is_active",)  # "Status" already covers it in the UI
        return context


class OrganizationDetailView(OrganizationScopedMixin, LoginRequiredMixin, DetailView):
    """Organization overview with counts, branding and recent activity."""

    template_name = "organizations/organization_detail.html"
    context_object_name = "organization"

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        context = super().get_context_data(**kwargs)
        organization = self.object
        user = self.request.user
        context["can_manage"] = can_manage_organization(user, organization)
        context["can_change_status"] = can_change_organization_status(user, organization)
        context["membership_count"] = organization.memberships.filter(is_active=True).count()
        context["location_count"] = organization.locations.count()
        context["location_type_count"] = organization.location_types.count()
        context["recent_activity"] = recent_activity(organization_id=organization.pk, limit=10)
        context["status_choices"] = Organization.Status.choices
        return context


class OrganizationCreateView(PolicyMixin, CreateView):
    """Create an organization through the service layer."""

    model = Organization
    form_class = OrganizationForm
    template_name = "organizations/organization_form.html"

    def policy_allows(self, obj=None) -> bool:  # noqa: ANN001
        return can_create_organization(self.request.user)

    def form_valid(self, form):  # noqa: ANN001, ANN201
        try:
            organization = services.create_organization(
                actor=self.request.user, **form.cleaned_data
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        self.object = organization
        messages.success(self.request, f"Organization “{organization.name}” created.")
        return HttpResponseRedirect(organization.get_absolute_url())


class OrganizationUpdateView(OrganizationScopedMixin, PolicyMixin, UpdateView):
    """Edit an organization's details through the service layer."""

    form_class = OrganizationForm
    template_name = "organizations/organization_form.html"
    policy_uses_object = True

    def policy_allows(self, obj=None) -> bool:  # noqa: ANN001
        return can_manage_organization(self.request.user, obj)

    def form_valid(self, form):  # noqa: ANN001, ANN201
        try:
            organization = services.update_organization(
                self.object, actor=self.request.user, **form.cleaned_data
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        self.object = organization
        messages.success(self.request, f"Organization “{organization.name}” updated.")
        return HttpResponseRedirect(organization.get_absolute_url())


class OrganizationBrandingView(OrganizationUpdateView):
    """Logo, favicon and brand colour (validated uploads)."""

    form_class = OrganizationBrandingForm
    template_name = "organizations/organization_branding.html"


class OrganizationStatusView(OrganizationScopedMixin, PolicyMixin, SingleObjectMixin, View):
    """Activate / deactivate / suspend an organization (POST only)."""

    policy_uses_object = True

    def policy_allows(self, obj=None) -> bool:  # noqa: ANN001
        return can_change_organization_status(self.request.user, obj)

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:  # noqa: ANN002, ANN003
        organization = self.get_object()
        form = OrganizationStatusForm(request.POST)
        if form.is_valid():
            organization = services.set_organization_status(
                organization, form.cleaned_data["status"], actor=request.user
            )
            messages.success(
                request,
                f"Organization “{organization.name}” is now {organization.get_status_display().lower()}.",
            )
        else:
            messages.error(request, "Choose a valid organization status.")
        return HttpResponseRedirect(reverse("organizations:detail", kwargs={"pk": organization.pk}))
