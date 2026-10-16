"""django-filter filtersets for checklist templates.

The filterset is applied *on top of* ``selectors.templates_for_user``, so a filter can never reach
another organization's templates; its relation choices come from the same authorized scope.
"""

from __future__ import annotations

import django_filters
from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.core.forms import DesignSystemFilterForm

from . import selectors
from .constants import TemplateStatus
from .models import ChecklistTemplate


def _categories(request):  # noqa: ANN001, ANN202
    return selectors.categories_for_user(request.user) if request else selectors.ChecklistCategory.objects.none()


def _creators(request):  # noqa: ANN001, ANN202
    users = get_user_model().objects
    if not request:
        return users.none()
    return users.filter(pk__in=selectors.templates_for_user(request.user).values("created_by_id")).order_by("email")


class TemplateFilter(django_filters.FilterSet):
    q = django_filters.CharFilter(method="filter_search", label="Search")
    status = django_filters.ChoiceFilter(choices=TemplateStatus.choices, label="Status")
    category = django_filters.ModelChoiceFilter(queryset=_categories, label="Category")
    created_by = django_filters.ModelChoiceFilter(queryset=_creators, label="Created by")
    updated = django_filters.DateFromToRangeFilter(
        field_name="updated_at", label="Updated between",
        widget=django_filters.widgets.RangeWidget(attrs={"type": "date", "class": "input"}),
    )
    version = django_filters.NumberFilter(method="filter_version", label="Latest version", min_value=1)

    class Meta:
        model = ChecklistTemplate
        form = DesignSystemFilterForm
        fields = ("q", "status", "category", "created_by", "updated", "version")

    def filter_search(self, queryset, name, value):  # noqa: ANN001, ANN201
        return queryset.filter(
            Q(name__icontains=value) | Q(code__icontains=value) | Q(description__icontains=value)
            | Q(category__name__icontains=value)
        )

    def filter_version(self, queryset, name, value):  # noqa: ANN001, ANN201
        return queryset.filter(latest_version_number=int(value))
