"""django-filter filtersets for the organization domain."""

from __future__ import annotations

import django_filters
from django.db.models import Q

from .models import Organization


class OrganizationFilter(django_filters.FilterSet):
    """Search and narrow the organization list."""

    q = django_filters.CharFilter(method="filter_search", label="Search")

    class Meta:
        model = Organization
        fields = ("status", "country")

    def filter_search(self, queryset, name, value):  # noqa: ANN001, ANN201
        return queryset.filter(
            Q(name__icontains=value)
            | Q(code__icontains=value)
            | Q(legal_name__icontains=value)
        )
