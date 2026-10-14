"""Class-based views for the organization domain.

Every application view is class-based.  Reads are scoped through the selector
layer and writes go through the service layer, so the same rules apply to the
HTML UI, the API and the admin.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView
from django.views.generic.edit import CreateView, UpdateView
from django_filters.views import FilterView
from django_tables2 import SingleTableMixin

from apps.core.policies import is_platform_staff
from apps.core.selectors import recent_activity

from . import services
from .filters import OrganizationFilter
from .forms import OrganizationForm, OrganizationStatusForm
from .models import Organization
from .policies import can_manage_organization
from .selectors import organizations_visible_to
from .tables import OrganizationTable


class OrganizationListView(LoginRequiredMixin, SingleTableMixin, FilterView):
    """List organizations visible to the current user."""

    table_class = OrganizationTable
    filterset_class = OrganizationFilter
    template_name = "organizations/organization_list.html"
    context_object_name = "organizations"

    def get_queryset(self):  # noqa: ANN201
        return organizations_visible_to(self.request.user)

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["can_create"] = is_platform_staff(user) or user.has_perm(
            "organizations.add_organization"
        )
        return context


class OrganizationDetailView(LoginRequiredMixin, DetailView):
    """Read-only organization overview."""

    template_name = "organizations/organization_detail.html"
    context_object_name = "organization"

    def get_queryset(self):  # noqa: ANN201
        return organizations_visible_to(self.request.user)

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        context = super().get_context_data(**kwargs)
        organization = self.object
        context["can_manage"] = can_manage_organization(self.request.user, organization)
        context["membership_count"] = organization.memberships.count()
        context["location_count"] = organization.locations.count()
        context["location_type_count"] = organization.location_types.count()
        context["recent_activity"] = recent_activity(organization_id=organization.pk, limit=10)
        return context


class OrganizationCreateView(LoginRequiredMixin, PermissionRequiredMixin, CreateView):
    """Create an organization through the service layer."""

    model = Organization
    form_class = OrganizationForm
    template_name = "organizations/organization_form.html"
    permission_required = "organizations.add_organization"

    def form_valid(self, form):  # noqa: ANN001, ANN201
        organization = services.create_organization(actor=self.request.user, **form.cleaned_data)
        self.object = organization
        messages.success(self.request, f"Organization \u201c{organization.name}\u201d created.")
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):  # noqa: ANN201
        return reverse("organizations:detail", kwargs={"pk": self.object.pk})


class OrganizationUpdateView(LoginRequiredMixin, PermissionRequiredMixin, UpdateView):
    """Edit an organization through the service layer."""

    model = Organization
    form_class = OrganizationForm
    template_name = "organizations/organization_form.html"
    permission_required = "organizations.change_organization"

    def get_queryset(self):  # noqa: ANN201
        return organizations_visible_to(self.request.user)

    def form_valid(self, form):  # noqa: ANN001, ANN201
        organization = services.update_organization(
            self.object, actor=self.request.user, **form.cleaned_data
        )
        self.object = organization
        messages.success(self.request, f"Organization \u201c{organization.name}\u201d updated.")
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):  # noqa: ANN201
        return reverse("organizations:detail", kwargs={"pk": self.object.pk})


class OrganizationStatusView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Activate / deactivate / suspend an organization (POST only)."""

    permission_required = "organizations.change_organization"

    def get_queryset(self):  # noqa: ANN201
        return organizations_visible_to(self.request.user)

    def post(self, request: HttpRequest, pk) -> HttpResponse:  # noqa: ANN001
        organization = get_object_or_404(self.get_queryset(), pk=pk)
        form = OrganizationStatusForm(request.POST)
        if form.is_valid():
            services.set_organization_status(
                organization, form.cleaned_data["status"], actor=request.user
            )
            messages.success(
                request,
                f"Organization \u201c{organization.name}\u201d is now "
                f"{organization.get_status_display().lower()}.",
            )
        else:
            messages.error(request, "Unknown organization status.")
        return HttpResponseRedirect(reverse("organizations:detail", kwargs={"pk": pk}))
