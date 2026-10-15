"""django-filter filtersets for themes (shared by the HTML list and the API list)."""

from __future__ import annotations

import django_filters
from django.db.models import Q

from apps.core.forms import DesignSystemFilterForm

from .constants import ThemeScope, ThemeStatus
from .models import Theme


class ThemeFilter(django_filters.FilterSet):
    q = django_filters.CharFilter(method="filter_search", label="Search")
    status = django_filters.ChoiceFilter(choices=ThemeStatus.choices, label="Status")
    scope = django_filters.ChoiceFilter(choices=ThemeScope.choices, label="Scope")
    organization = django_filters.ModelChoiceFilter(
        queryset=lambda request: _organizations(request), label="Organization"
    )

    class Meta:
        model = Theme
        form = DesignSystemFilterForm
        fields = ("q", "status", "scope", "organization")

    def filter_search(self, queryset, name, value):  # noqa: ANN001, ANN201
        return queryset.filter(Q(name__icontains=value) | Q(code__icontains=value))


def _organizations(request):  # noqa: ANN001, ANN202
    """The filter's choices are limited to organizations the user can see (no tenant leak)."""
    from apps.organizations.selectors import organizations_visible_to

    return organizations_visible_to(getattr(request, "user", None))
