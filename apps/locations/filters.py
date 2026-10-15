"""django-filter filtersets for the location domain.

These filtersets are shared by the HTML pages and the REST API, so a filter means
exactly the same thing in both.  Every relation filter (organization, type,
parent) is *scoped to what the requesting user may see*: its choices - and what
it will accept - come from the same selectors as the lists themselves, so a
filter can never reveal or match another tenant's data.
"""

from __future__ import annotations

import django_filters
from django.db.models import Q

from apps.core.forms import DesignSystemFilterForm, YesNoAnyWidget
from apps.organizations.models import Organization
from apps.organizations.selectors import organizations_visible_to

from .models import Location, LocationType
from .selectors import location_types_visible_to, locations_visible_to
from .widgets import LocationPickerWidget


def _visible_organizations(request):  # noqa: ANN001, ANN202
    return organizations_visible_to(request.user) if request else Organization.objects.none()


def _visible_types(request):  # noqa: ANN001, ANN202
    return (
        location_types_visible_to(request.user).select_related("organization")
        if request
        else LocationType.objects.none()
    )


def _visible_locations(request):  # noqa: ANN001, ANN202
    return locations_visible_to(request.user) if request else Location.objects.none()


STATUS_CHOICES = (("active", "Active"), ("inactive", "Inactive"))


class LocationTypeFilter(django_filters.FilterSet):
    """Search and narrow the location-type list."""

    q = django_filters.CharFilter(method="filter_search", label="Search")
    organization = django_filters.ModelChoiceFilter(
        queryset=_visible_organizations, label="Organization"
    )
    status = django_filters.ChoiceFilter(
        choices=STATUS_CHOICES, method="filter_status", label="Status"
    )
    is_active = django_filters.BooleanFilter(label="Active", widget=YesNoAnyWidget)

    class Meta:
        model = LocationType
        form = DesignSystemFilterForm
        fields = ("q", "organization", "status", "is_active")

    def filter_search(self, queryset, name, value):  # noqa: ANN001, ANN201
        return queryset.filter(Q(name__icontains=value) | Q(code__icontains=value))

    def filter_status(self, queryset, name, value):  # noqa: ANN001, ANN201
        return queryset.filter(is_active=(value == "active"))


class LocationFilter(django_filters.FilterSet):
    """Search and narrow the location list / tree."""

    q = django_filters.CharFilter(method="filter_search", label="Search")
    organization = django_filters.ModelChoiceFilter(
        queryset=_visible_organizations, label="Organization"
    )
    location_type = django_filters.ModelChoiceFilter(queryset=_visible_types, label="Type")
    parent = django_filters.ModelChoiceFilter(
        queryset=_visible_locations,
        label="Parent",
        widget=LocationPickerWidget(include_inactive=True, organization_source="[name=organization]"),
    )
    root = django_filters.BooleanFilter(
        field_name="parent", lookup_expr="isnull", label="Top level only", widget=YesNoAnyWidget
    )
    status = django_filters.ChoiceFilter(
        choices=STATUS_CHOICES, method="filter_status", label="Status"
    )
    is_active = django_filters.BooleanFilter(label="Active", widget=YesNoAnyWidget)
    country = django_filters.CharFilter(lookup_expr="iexact", label="Country")
    city = django_filters.CharFilter(lookup_expr="icontains", label="City")

    class Meta:
        model = Location
        form = DesignSystemFilterForm
        fields = (
            "q",
            "organization",
            "location_type",
            "parent",
            "root",
            "status",
            "is_active",
            "country",
            "city",
        )

    def filter_search(self, queryset, name, value):  # noqa: ANN001, ANN201
        return queryset.filter(
            Q(name__icontains=value) | Q(code__icontains=value) | Q(city__icontains=value)
        )

    def filter_status(self, queryset, name, value):  # noqa: ANN001, ANN201
        return queryset.filter(is_active=(value == "active"))
