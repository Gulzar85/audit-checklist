"""django-filter filtersets for the organization domain.

Shared by the HTML list and the API list so both filter identically.
"""

from __future__ import annotations

import django_filters
from django.db.models import Q

from apps.core.forms import DesignSystemFilterForm, YesNoAnyWidget

from .models import Organization


class OrganizationFilter(django_filters.FilterSet):
    """Search and narrow the organization list."""

    q = django_filters.CharFilter(method="filter_search", label="Search")
    status = django_filters.ChoiceFilter(choices=Organization.Status.choices, label="Status")
    is_active = django_filters.BooleanFilter(method="filter_is_active", label="Active", widget=YesNoAnyWidget)
    country = django_filters.CharFilter(lookup_expr="iexact", label="Country")

    class Meta:
        model = Organization
        form = DesignSystemFilterForm
        fields = ("q", "status", "is_active", "country")

    def filter_search(self, queryset, name, value):  # noqa: ANN001, ANN201
        return queryset.filter(
            Q(name__icontains=value) | Q(code__icontains=value) | Q(legal_name__icontains=value)
        )

    def filter_is_active(self, queryset, name, value):  # noqa: ANN001, ANN201
        # ``is_active`` is derived from ``status`` (ADR 0011), not a column.
        active = Q(status=Organization.Status.ACTIVE)
        return queryset.filter(active) if value else queryset.exclude(active)
