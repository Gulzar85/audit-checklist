"""django-filter filterset for the findings list: applied *on top of* ``selectors.findings_visible_to``.

Relation choices are drawn from the same authorized queryset, so a filter can never name - or reveal - something outside the
caller's scope.  ``status`` accepts the derived ``overdue`` as well as the persisted statuses; ``location`` includes descendants.
"""

from __future__ import annotations

import django_filters
from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.core.forms import DesignSystemFilterForm
from apps.locations.models import Location

from . import selectors
from .constants import OVERDUE, FindingStatus, SourceType
from .models import Finding, FindingCategory, FindingSeverity

STATUS_CHOICES = [*FindingStatus.choices, (OVERDUE, "Overdue")]
_DATE = django_filters.widgets.RangeWidget(attrs={"type": "date", "class": "input"})


def _scope(request):  # noqa: ANN001, ANN202
    return selectors.findings_visible_to(request.user) if request else Finding.objects.none()


def _locations(request):  # noqa: ANN001, ANN202
    """Locations with findings in scope, plus the locations the user may view findings at (so a parent can be chosen to include its subtree)."""
    if not request:
        return Location.objects.none()
    from apps.permissions.authorization import get_accessible_locations
    from apps.permissions.registry import P

    return Location.objects.filter(Q(pk__in=_scope(request).values("location_id")) | Q(pk__in=get_accessible_locations(request.user, P.FINDING_VIEW).values("pk"))).order_by("name")


def _categories(request):  # noqa: ANN001, ANN202
    return FindingCategory.objects.filter(pk__in=_scope(request).values("category_id")).order_by("sort_order", "name")


def _severities(request):  # noqa: ANN001, ANN202
    return FindingSeverity.objects.filter(pk__in=_scope(request).values("severity_id")).order_by("-rank")


def _owners(request):  # noqa: ANN001, ANN202
    return get_user_model().objects.filter(pk__in=_scope(request).values("assigned_to_id")).order_by("email")


class FindingFilter(django_filters.FilterSet):
    q = django_filters.CharFilter(method="filter_search", label="Search")
    status = django_filters.ChoiceFilter(choices=STATUS_CHOICES, method="filter_status", label="Status")
    severity = django_filters.ModelChoiceFilter(queryset=_severities, label="Severity")
    category = django_filters.ModelChoiceFilter(queryset=_categories, label="Category")
    source_type = django_filters.ChoiceFilter(choices=SourceType.choices, label="Source")
    location = django_filters.ModelChoiceFilter(queryset=_locations, method="filter_location", label="Location (with sub-locations)")
    assigned_to = django_filters.ModelChoiceFilter(queryset=_owners, label="Responsible")
    detected = django_filters.DateFromToRangeFilter(field_name="detected_at", label="Detected between", widget=_DATE)
    due = django_filters.DateFromToRangeFilter(field_name="due_at", label="Due between", widget=_DATE)

    class Meta:
        model = Finding
        form = DesignSystemFilterForm
        fields = ("q", "status", "severity", "category", "source_type", "location", "assigned_to", "detected", "due")

    def filter_search(self, queryset, name, value):  # noqa: ANN001, ANN201
        digits = "".join(ch for ch in value if ch.isdigit())
        number = Q(number=int(digits)) if digits and (value.upper().startswith(("FND", "#")) or value.isdigit()) else Q()
        return queryset.filter(number | Q(title__icontains=value) | Q(description__icontains=value) | Q(location__name__icontains=value))

    def filter_status(self, queryset, name, value):  # noqa: ANN001, ANN201
        return selectors.overdue(queryset) if value == OVERDUE else queryset.filter(status=value)

    def filter_location(self, queryset, name, value):  # noqa: ANN001, ANN201
        from apps.locations.selectors import get_descendants

        return queryset.filter(location__in=get_descendants(value, include_self=True).values("pk"))
