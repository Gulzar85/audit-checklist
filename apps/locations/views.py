"""Class-based views for the location domain.

Reads are scoped through the selector layer; every write goes through the
service layer (which validates the hierarchy, locks rows and logs activity).
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView, TemplateView
from django.views.generic.edit import CreateView, UpdateView
from django_filters.views import FilterView
from django_tables2 import SingleTableMixin

from apps.core.policies import is_platform_staff
from apps.organizations.selectors import organizations_visible_to

from . import services
from .filters import LocationFilter, LocationTypeFilter
from .forms import LocationForm, LocationStatusForm, LocationTypeForm
from .models import Location, LocationType
from .policies import can_manage_location
from .selectors import location_tree, locations_visible_to
from .tables import LocationTable, LocationTypeTable


# ---------------------------------------------------------------------------
# Location types
# ---------------------------------------------------------------------------
class LocationTypeListView(LoginRequiredMixin, SingleTableMixin, FilterView):
    """List location types visible to the current user."""

    table_class = LocationTypeTable
    filterset_class = LocationTypeFilter
    template_name = "locations/locationtype_list.html"
    context_object_name = "location_types"
    table_pagination = {"per_page": 20}

    def get_queryset(self):  # noqa: ANN201
        visible = organizations_visible_to(self.request.user)
        return (
            LocationType.objects.filter(Q(organization__in=visible) | Q(organization__isnull=True))
            .select_related("organization")
        )

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        context = super().get_context_data(**kwargs)
        context["can_create"] = self.request.user.has_perm("locations.add_locationtype")
        return context


class LocationTypeCreateView(LoginRequiredMixin, PermissionRequiredMixin, CreateView):
    model = LocationType
    form_class = LocationTypeForm
    template_name = "locations/locationtype_form.html"
    permission_required = "locations.add_locationtype"

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):  # noqa: ANN001, ANN201
        location_type = services.create_location_type(
            actor=self.request.user, **form.cleaned_data
        )
        self.object = location_type
        messages.success(self.request, f"Location type \u201c{location_type.name}\u201d created.")
        return HttpResponseRedirect(reverse("locations:type_list"))


class LocationTypeUpdateView(LoginRequiredMixin, PermissionRequiredMixin, UpdateView):
    model = LocationType
    form_class = LocationTypeForm
    template_name = "locations/locationtype_form.html"
    permission_required = "locations.change_locationtype"

    def get_queryset(self):  # noqa: ANN201
        visible = organizations_visible_to(self.request.user)
        return LocationType.objects.filter(
            Q(organization__in=visible) | Q(organization__isnull=True)
        )

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):  # noqa: ANN001, ANN201
        location_type = services.update_location_type(
            self.object, actor=self.request.user, **form.cleaned_data
        )
        self.object = location_type
        messages.success(self.request, f"Location type \u201c{location_type.name}\u201d updated.")
        return HttpResponseRedirect(reverse("locations:type_list"))


class LocationTypeStatusView(LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "locations.change_locationtype"

    def get_queryset(self):  # noqa: ANN201
        visible = organizations_visible_to(self.request.user)
        return LocationType.objects.filter(
            Q(organization__in=visible) | Q(organization__isnull=True)
        )

    def post(self, request: HttpRequest, pk) -> HttpResponse:  # noqa: ANN001
        location_type = get_object_or_404(self.get_queryset(), pk=pk)
        action = request.POST.get("action")
        services.set_location_type_active(
            location_type, action == "activate", actor=request.user
        )
        messages.success(
            request,
            f"Location type \u201c{location_type.name}\u201d "
            f"{'activated' if action == 'activate' else 'deactivated'}.",
        )
        return HttpResponseRedirect(reverse("locations:type_list"))


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
class LocationListView(LoginRequiredMixin, SingleTableMixin, FilterView):
    """Filterable, paginated location list."""

    table_class = LocationTable
    filterset_class = LocationFilter
    template_name = "locations/location_list.html"
    context_object_name = "locations"
    table_pagination = {"per_page": 20}

    def get_queryset(self):  # noqa: ANN201
        return locations_visible_to(self.request.user).select_related(
            "organization", "location_type", "parent"
        )

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        context = super().get_context_data(**kwargs)
        context["organizations"] = organizations_visible_to(self.request.user)
        context["can_create"] = self.request.user.has_perm("locations.add_location")
        return context


class LocationTreeView(LoginRequiredMixin, TemplateView):
    """Hierarchical, expandable location tree."""

    template_name = "locations/location_tree.html"

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        context = super().get_context_data(**kwargs)
        user = self.request.user
        visible = organizations_visible_to(user)
        organization_id = self.request.GET.get("organization") or None
        visible_ids = None if is_platform_staff(user) else list(visible.values_list("pk", flat=True))
        if organization_id and visible_ids is not None and organization_id not in {
            str(i) for i in visible_ids
        }:
            organization_id = None
        context["roots"] = location_tree(
            organization_id=organization_id,
            visible_organization_ids=visible_ids,
        )
        context["organizations"] = visible
        context["selected_organization"] = organization_id
        context["can_create"] = user.has_perm("locations.add_location")
        return context


class LocationDetailView(LoginRequiredMixin, DetailView):
    template_name = "locations/location_detail.html"
    context_object_name = "location"

    def get_queryset(self):  # noqa: ANN201
        return locations_visible_to(self.request.user).select_related(
            "organization", "location_type", "parent"
        )

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        context = super().get_context_data(**kwargs)
        location = self.object
        context["ancestors"] = location.get_ancestors()
        context["children"] = location.children.select_related("location_type").order_by("name")
        context["descendant_count"] = location.get_descendants().count()
        context["can_manage"] = can_manage_location(self.request.user, location)
        return context


class LocationCreateView(LoginRequiredMixin, PermissionRequiredMixin, CreateView):
    model = Location
    form_class = LocationForm
    template_name = "locations/location_form.html"
    permission_required = "locations.add_location"

    def get_initial(self):  # noqa: ANN201
        initial = super().get_initial()
        organization_id = self.request.GET.get("organization")
        if organization_id:
            organization = organizations_visible_to(self.request.user).filter(
                pk=organization_id
            ).first()
            if organization:
                initial["organization"] = organization
        parent_id = self.request.GET.get("parent")
        if parent_id:
            initial["parent"] = parent_id
        return initial

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        organization_id = self.request.GET.get("organization")
        if organization_id:
            kwargs["organization"] = organizations_visible_to(self.request.user).filter(
                pk=organization_id
            ).first()
        return kwargs

    def form_valid(self, form):  # noqa: ANN001, ANN201
        location = services.create_location(actor=self.request.user, **form.cleaned_data)
        self.object = location
        messages.success(self.request, f"Location \u201c{location.name}\u201d created.")
        return HttpResponseRedirect(location.get_absolute_url())


class LocationUpdateView(LoginRequiredMixin, PermissionRequiredMixin, UpdateView):
    model = Location
    form_class = LocationForm
    template_name = "locations/location_form.html"
    permission_required = "locations.change_location"

    def get_queryset(self):  # noqa: ANN201
        return locations_visible_to(self.request.user)

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["organization"] = self.object.organization
        return kwargs

    def form_valid(self, form):  # noqa: ANN001, ANN201
        data = dict(form.cleaned_data)
        new_parent = data.pop("parent", None)
        data.pop("organization", None)  # an existing location never changes tenant
        actor = self.request.user

        location = self.object
        if new_parent != location.parent:
            location = services.move_location(location, new_parent, actor=actor)
        location = services.update_location(location, actor=actor, **data)

        self.object = location
        messages.success(self.request, f"Location \u201c{location.name}\u201d updated.")
        return HttpResponseRedirect(location.get_absolute_url())


class LocationStatusView(LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "locations.change_location"

    def get_queryset(self):  # noqa: ANN201
        return locations_visible_to(self.request.user)

    def post(self, request: HttpRequest, pk) -> HttpResponse:  # noqa: ANN001
        location = get_object_or_404(self.get_queryset(), pk=pk)
        form = LocationStatusForm(request.POST)
        if form.is_valid():
            if form.cleaned_data["action"] == "activate":
                services.activate_location(location, actor=request.user)
                messages.success(request, f"Location \u201c{location.name}\u201d activated.")
            else:
                services.deactivate_location(location, actor=request.user)
                messages.success(request, f"Location \u201c{location.name}\u201d deactivated.")
        else:
            messages.error(request, "Unknown location action.")
        return HttpResponseRedirect(reverse("locations:detail", kwargs={"pk": pk}))


class LocationRelationsView(LoginRequiredMixin, View):
    """HTMX partial: parent/location-type selects scoped to an organization."""

    def get(self, request: HttpRequest) -> HttpResponse:
        visible = organizations_visible_to(request.user)
        organization = None
        organization_id = request.GET.get("organization")
        if organization_id:
            organization = visible.filter(pk=organization_id).first()

        instance = None
        instance_id = request.GET.get("instance")
        if instance_id:
            instance = get_object_or_404(
                Location.objects.filter(organization__in=visible), pk=instance_id
            )

        initial: dict[str, object] = {}
        if request.GET.get("parent"):
            initial["parent"] = request.GET["parent"]
        if request.GET.get("location_type"):
            initial["location_type"] = request.GET["location_type"]

        form = LocationForm(
            user=request.user,
            organization=organization,
            instance=instance,
            initial=initial,
        )
        return render(request, "locations/_relations_fields.html", {"form": form})
