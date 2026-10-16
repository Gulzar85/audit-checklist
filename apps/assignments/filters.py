"""django-filter filterset for the assignment list: applied *on top of* ``selectors.assignments_visible_to``.

Relation choices are drawn from the same authorized queryset, so a filter can never name - or reveal - something outside the
caller's scope.  ``status`` accepts the derived ``overdue`` as well as the persisted statuses.
"""

from __future__ import annotations

import django_filters
from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.checklists.models import ChecklistTemplate
from apps.core.forms import DesignSystemFilterForm
from apps.locations.models import Location

from . import selectors
from .constants import OVERDUE, AssignmentPriority, AssignmentStatus
from .models import ChecklistAssignment

STATUS_CHOICES = [*AssignmentStatus.choices, (OVERDUE, "Overdue")]


def _scope(request):  # noqa: ANN001, ANN202
    return selectors.assignments_visible_to(request.user) if request else ChecklistAssignment.objects.none()


def _templates(request):  # noqa: ANN001, ANN202
    return ChecklistTemplate.objects.filter(pk__in=_scope(request).values("version__template_id")).order_by("name")


def _locations(request):  # noqa: ANN001, ANN202
    return Location.objects.filter(pk__in=_scope(request).values("location_id")).order_by("name")


def _users(field):  # noqa: ANN001, ANN202
    def queryset(request):  # noqa: ANN001, ANN202
        return get_user_model().objects.filter(pk__in=_scope(request).values(field)).order_by("email")

    return queryset


class AssignmentFilter(django_filters.FilterSet):
    q = django_filters.CharFilter(method="filter_search", label="Search")
    status = django_filters.ChoiceFilter(choices=STATUS_CHOICES, method="filter_status", label="Status")
    priority = django_filters.ChoiceFilter(choices=AssignmentPriority.choices, label="Priority")
    template = django_filters.ModelChoiceFilter(queryset=_templates, field_name="version__template", label="Checklist")
    version = django_filters.NumberFilter(field_name="version__version_number", label="Version", min_value=1)
    location = django_filters.ModelChoiceFilter(queryset=_locations, label="Location")
    assigned_to = django_filters.ModelChoiceFilter(queryset=_users("assigned_to_id"), label="Responsible user")
    created_by = django_filters.ModelChoiceFilter(queryset=_users("created_by_id"), label="Created by")
    assigned = django_filters.DateFromToRangeFilter(
        field_name="issued_at", label="Assigned between", widget=django_filters.widgets.RangeWidget(attrs={"type": "date", "class": "input"}))
    due = django_filters.DateFromToRangeFilter(
        field_name="due_at", label="Due between", widget=django_filters.widgets.RangeWidget(attrs={"type": "date", "class": "input"}))

    class Meta:
        model = ChecklistAssignment
        form = DesignSystemFilterForm
        fields = ("q", "status", "priority", "template", "version", "location", "assigned_to", "created_by", "assigned", "due")

    def filter_search(self, queryset, name, value):  # noqa: ANN001, ANN201
        digits = "".join(ch for ch in value if ch.isdigit())
        number = Q(number=int(digits)) if digits and value.upper().startswith(("ASG", "#")) or value.isdigit() else Q()
        return queryset.filter(
            number | Q(version__template__name__icontains=value) | Q(version__template__code__icontains=value)
            | Q(location__name__icontains=value) | Q(location__code__icontains=value)
        )

    def filter_status(self, queryset, name, value):  # noqa: ANN001, ANN201
        return selectors.by_status(queryset, value)
