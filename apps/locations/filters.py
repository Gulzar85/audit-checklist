"""django-filter filtersets for the location domain."""

from __future__ import annotations

import django_filters
from django.db.models import Q

from .models import Location, LocationType


class LocationTypeFilter(django_filters.FilterSet):
    """Search and narrow the location-type list."""

    q = django_filters.CharFilter(method="filter_search", label="Search")

    class Meta:
        model = LocationType
        fields = ("organization", "is_active")

    def filter_search(self, queryset, name, value):  # noqa: ANN001, ANN201
        return queryset.filter(Q(name__icontains=value) | Q(code__icontains=value))


class LocationFilter(django_filters.FilterSet):
    """Search and narrow the location list."""

    q = django_filters.CharFilter(method="filter_search", label="Search")

    class Meta:
        model = Location
        fields = ("organization", "location_type", "parent", "is_active", "country", "city")

    def filter_search(self, queryset, name, value):  # noqa: ANN001, ANN201
        return queryset.filter(
            Q(name__icontains=value) | Q(code__icontains=value) | Q(city__icontains=value)
        )
