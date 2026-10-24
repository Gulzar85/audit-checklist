"""Class-based views for the location domain.

Reads are scoped through the selector layer (outside the user's organizations a
location is simply a 404); authorization goes through ``policies``; every write
goes through ``services`` (which validate the hierarchy, lock, and log).
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.cache import patch_vary_headers
from django.views import View
from django.views.generic import DetailView, TemplateView
from django.views.generic.detail import SingleObjectMixin
from django.views.generic.edit import CreateView, UpdateView
from django_filters.views import FilterView
from django_tables2 import SingleTableMixin

from apps.core.forms import add_validation_error
from apps.core.mixins import PolicyMixin
from apps.core.utils import is_htmx, parse_uuid
from apps.organizations.selectors import organizations_visible_to

from . import services
from .filters import LocationFilter, LocationTypeFilter
from .forms import (
    LocationForm,
    LocationStatusForm,
    LocationTypeForm,
    LocationTypeStatusForm,
)
from .models import Location, LocationType
from .policies import (
    can_activate_location,
    can_create_any_location_type,
    can_create_location,
    can_deactivate_location,
    can_edit_location,
    can_edit_location_type,
    can_move_location,
)
from .selectors import (
    flatten_tree,
    get_children,
    get_descendants,
    get_location_tree,
    location_types_visible_to,
    locations_visible_to,
)
from .tables import LocationTable, LocationTypeTable

PICKER_LIMIT = 20


# ---------------------------------------------------------------------------
# Location types
# ---------------------------------------------------------------------------
class LocationTypeScopedMixin:
    def get_queryset(self):  # noqa: ANN201
        return location_types_visible_to(self.request.user).select_related("organization")


class LocationTypeListView(LoginRequiredMixin, SingleTableMixin, FilterView):
    """Location types visible to the current user (their organizations' + platform)."""

    table_class = LocationTypeTable
    filterset_class = LocationTypeFilter
    template_name = "locations/locationtype_list.html"
    context_object_name = "location_types"
    table_pagination = {"per_page": 20}

    def get_queryset(self):  # noqa: ANN201
        return location_types_visible_to(self.request.user).select_related("organization")

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        context = super().get_context_data(**kwargs)
        context["can_create"] = can_create_any_location_type(self.request.user)
        context["hidden_filters"] = ("is_active",)  # "Status" already covers it in the UI
        return context


class LocationTypeCreateView(PolicyMixin, CreateView):
    model = LocationType
    form_class = LocationTypeForm
    template_name = "locations/locationtype_form.html"

    def policy_allows(self, obj=None) -> bool:  # noqa: ANN001
        return can_create_any_location_type(self.request.user)

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):  # noqa: ANN001, ANN201
        try:
            location_type = services.create_location_type(
                actor=self.request.user, **form.cleaned_data
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, f"Location type “{location_type.name}” created.")
        return HttpResponseRedirect(reverse("locations:type_list"))


class LocationTypeUpdateView(LocationTypeScopedMixin, PolicyMixin, UpdateView):
    form_class = LocationTypeForm
    template_name = "locations/locationtype_form.html"
    policy_uses_object = True

    def policy_allows(self, obj=None) -> bool:  # noqa: ANN001
        return can_edit_location_type(self.request.user, obj)

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):  # noqa: ANN001, ANN201
        try:
            location_type = services.update_location_type(
                self.object, actor=self.request.user, **form.cleaned_data
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, f"Location type “{location_type.name}” updated.")
        return HttpResponseRedirect(reverse("locations:type_list"))


class LocationTypeStatusView(LocationTypeScopedMixin, PolicyMixin, SingleObjectMixin, View):
    """Activate or deactivate a location type (POST only)."""

    policy_uses_object = True

    def policy_allows(self, obj=None) -> bool:  # noqa: ANN001
        return can_edit_location_type(self.request.user, obj)

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:  # noqa: ANN002, ANN003
        location_type = self.get_object()
        form = LocationTypeStatusForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Unknown action.")
        else:
            activate = form.cleaned_data["action"] == "activate"
            try:
                if activate:
                    services.activate_location_type(location_type, actor=request.user)
                else:
                    services.deactivate_location_type(location_type, actor=request.user)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                messages.success(
                    request,
                    f"Location type “{location_type.name}” "
                    f"{'activated' if activate else 'deactivated'}.",
                )
        return HttpResponseRedirect(reverse("locations:type_list"))


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
class LocationScopedMixin:
    def get_queryset(self):  # noqa: ANN201
        return locations_visible_to(self.request.user).select_related(
            "organization", "location_type", "parent"
        )


class LocationListView(LoginRequiredMixin, SingleTableMixin, FilterView):
    """Filterable, sortable, paginated location list."""

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
        context["can_create"] = can_create_location(self.request.user)
        context["hidden_filters"] = ("is_active",)  # "Status" already covers it in the UI
        return context


class LocationTreeView(LoginRequiredMixin, FilterView):
    """The expandable hierarchy.  Built from a constant number of queries.

    The same filterset as the list applies; matches keep their ancestors for
    context.  HTMX requests receive only the tree panel, so searching and
    filtering swap the tree without reloading the page.
    """

    filterset_class = LocationFilter
    template_name = "locations/location_tree.html"
    partial_template_name = "locations/_tree_panel.html"
    context_object_name = "locations"

    def get_queryset(self):  # noqa: ANN201
        if not hasattr(self, "_scope"):  # resolved once per request (it runs a small RBAC lookup)
            self._scope = locations_visible_to(self.request.user)
        return self._scope

    def get_template_names(self):  # noqa: ANN201
        return [self.partial_template_name if is_htmx(self.request) else self.template_name]

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        context = super().get_context_data(**kwargs)
        form = context["filter"].form
        context["is_filtered"] = bool(
            form.is_bound
            and form.is_valid()
            and any(value not in (None, "", []) for value in form.cleaned_data.values())
        )
        roots = get_location_tree(self.object_list, within=self.get_queryset())
        context["roots"] = roots
        context["rows"], context["trailing_closers"] = flatten_tree(roots)
        context["show_organization"] = len({node.location.organization_id for node in roots}) > 1
        context["can_create"] = can_create_location(self.request.user)
        context["hidden_filters"] = ("is_active",)  # "Status" already covers it in the UI
        return context

    def render_to_response(self, context, **response_kwargs):  # noqa: ANN001, ANN003, ANN201
        response = super().render_to_response(context, **response_kwargs)
        patch_vary_headers(response, ("HX-Request",))
        return response


class LocationDetailView(LocationScopedMixin, LoginRequiredMixin, DetailView):
    template_name = "locations/location_detail.html"
    context_object_name = "location"

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        context = super().get_context_data(**kwargs)
        location = self.object
        user = self.request.user
        reachable = locations_visible_to(user).values("pk")  # never reveal what the role scope hides
        ancestors = location.get_ancestors()
        visible_ancestors = set(
            locations_visible_to(user).filter(pk__in=[a.pk for a in ancestors]).values_list("pk", flat=True)
        )
        context["ancestors"] = [a for a in ancestors if a.pk in visible_ancestors]
        context["parent_visible"] = location.parent_id in visible_ancestors
        context["children"] = get_children(location).filter(pk__in=reachable).select_related("location_type")
        context["descendant_count"] = get_descendants(location).filter(pk__in=reachable).count()
        context["active_descendant_count"] = get_descendants(location).filter(pk__in=reachable, is_active=True).count()
        context["can_edit"] = can_edit_location(user, location)
        context["can_create_child"] = can_create_location(user, parent=location)
        context["can_deactivate"] = can_deactivate_location(user, location)
        context["can_activate"] = can_activate_location(user, location)
        return context


class LocationFormMixin:
    """Shared plumbing for the create/update forms."""

    form_class = LocationForm
    template_name = "locations/location_form.html"

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        context = super().get_context_data(**kwargs)
        context["address_fields"] = LocationForm.address_fields
        return context


class LocationCreateView(LocationFormMixin, PolicyMixin, CreateView):
    model = Location

    def policy_allows(self, obj=None) -> bool:  # noqa: ANN001
        return can_create_location(self.request.user)

    def get_initial(self):  # noqa: ANN201
        """Pre-fill from ``?parent=`` ("add child") or ``?organization=``."""
        initial = super().get_initial()
        scope = locations_visible_to(self.request.user)
        parent_id = parse_uuid(self.request.GET.get("parent"))
        parent = scope.select_related("organization").filter(pk=parent_id).first() if parent_id else None
        if parent is not None:
            initial["organization"] = parent.organization
            initial["parent"] = parent.pk
            initial["location_type"] = parent.location_type_id
            return initial
        organization_id = parse_uuid(self.request.GET.get("organization"))
        if organization_id:
            organization = organizations_visible_to(self.request.user).filter(pk=organization_id).first()
            if organization is not None:
                initial["organization"] = organization
        else:
            visible = list(organizations_visible_to(self.request.user).filter(status="active")[:2])
            if len(visible) == 1:
                initial["organization"] = visible[0]
        return initial

    def form_valid(self, form):  # noqa: ANN001, ANN201
        try:
            location = services.create_location(actor=self.request.user, **form.cleaned_data)
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        self.object = location
        messages.success(self.request, f"Location “{location.name}” created.")
        return HttpResponseRedirect(location.get_absolute_url())


class LocationUpdateView(LocationScopedMixin, LocationFormMixin, PolicyMixin, UpdateView):
    policy_uses_object = True

    def policy_allows(self, obj=None) -> bool:  # noqa: ANN001
        return can_edit_location(self.request.user, obj)

    def form_valid(self, form):  # noqa: ANN001, ANN201
        new_parent = form.cleaned_data.get("parent")
        parent_changed = (new_parent.pk if new_parent else None) != (
            form.initial.get("parent") or None
        )
        if parent_changed and not can_move_location(self.request.user, self.object, new_parent):
            raise PermissionDenied
        try:
            location = services.update_location(
                self.object, actor=self.request.user, **form.cleaned_data
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        self.object = location
        messages.success(self.request, f"Location “{location.name}” updated.")
        return HttpResponseRedirect(location.get_absolute_url())


class LocationStatusView(LocationScopedMixin, PolicyMixin, SingleObjectMixin, View):
    """Activate or deactivate a location, optionally with its descendants (POST only)."""

    policy_uses_object = True

    def policy_allows(self, obj=None) -> bool:  # noqa: ANN001
        activating = self.request.POST.get("action") == "activate"
        check = can_activate_location if activating else can_deactivate_location
        return check(self.request.user, obj)

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:  # noqa: ANN002, ANN003
        location = self.get_object()
        form = LocationStatusForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Unknown location action.")
        else:
            try:
                if form.cleaned_data["action"] == "activate":
                    services.activate_location(location, actor=request.user)
                    messages.success(request, f"Location “{location.name}” activated.")
                else:
                    services.deactivate_location(
                        location, actor=request.user, cascade=form.cleaned_data["cascade"]
                    )
                    messages.success(request, f"Location “{location.name}” deactivated.")
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
        return HttpResponseRedirect(reverse("locations:detail", kwargs={"pk": location.pk}))


# ---------------------------------------------------------------------------
# HTMX partials
# ---------------------------------------------------------------------------
class LocationPickerView(LoginRequiredMixin, TemplateView):
    """Search results for the location picker widget (an HTML fragment).

    Always scoped to the user's visible organizations.  Optional parameters:
    ``organization`` (narrow), ``exclude`` (omit a location and its subtree),
    ``include_inactive`` (otherwise only active locations, which are the only
    valid parents), ``require_organization`` (ask the user to choose one first).
    """

    template_name = "locations/_picker_options.html"

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        context = super().get_context_data(**kwargs)
        params = self.request.GET
        term = (params.get("q") or "").strip()[:100]
        organization_id = parse_uuid(params.get("organization"))

        if params.get("require_organization") and not organization_id:
            context["needs_organization"] = True
            return context

        queryset = locations_visible_to(self.request.user).select_related(
            "location_type", "organization"
        )
        if organization_id:
            queryset = queryset.filter(organization_id=organization_id)
        if not params.get("include_inactive"):
            queryset = queryset.filter(is_active=True)
        exclude_id = parse_uuid(params.get("exclude"))
        if exclude_id:
            queryset = queryset.exclude(
                pk__in=get_descendants(exclude_id, include_self=True)
            )
        if term:
            queryset = queryset.filter(Q(name__icontains=term) | Q(code__icontains=term))

        results = list(queryset.order_by("name", "code")[: PICKER_LIMIT + 1])
        context["results"] = results[:PICKER_LIMIT]
        context["truncated"] = len(results) > PICKER_LIMIT
        context["show_organization"] = not organization_id
        return context


class LocationRelationsView(LoginRequiredMixin, View):
    """HTMX fragment: type + parent fields re-scoped when the organization changes."""

    def get(self, request: HttpRequest) -> HttpResponse:
        organization = None
        organization_id = parse_uuid(request.GET.get("organization"))
        if organization_id:
            organization = (
                organizations_visible_to(request.user).filter(pk=organization_id).first()
            )
        form = LocationForm(user=request.user, organization=organization)
        return render(request, "locations/_relations_fields.html", {"form": form})


class LocationBranchView(LocationScopedMixin, LoginRequiredMixin, View):
    """HTMX: the direct children of one location, for lazily-expanded tree branches.

    The parent is fetched through the scoped queryset (another organization's location is a 404), and
    only its own children are returned, so a large tree is loaded one level at a time.
    """

    def get(self, request: HttpRequest, pk) -> HttpResponse:  # noqa: ANN001
        from django.db.models import Count
        from django.shortcuts import get_object_or_404

        parent = get_object_or_404(self.get_queryset(), pk=pk)
        children = (
            Location.objects.filter(parent=parent)
            .select_related("location_type")
            .annotate(child_total=Count("children"))
            .order_by("name")
        )
        return render(request, "components/tree/branch.html", {"parent": parent, "nodes": [
            {
                "label": child.name,
                "meta": f"{child.location_type.name} - {child.code}",
                "href": reverse("locations:detail", args=[child.pk]),
                "status": "active" if child.is_active else "inactive",
                "has_children": child.child_total > 0,
                "children_url": reverse("locations:branch", args=[child.pk]),
                "children": [],
            }
            for child in children[:200]
        ]})
