"""django-filter filterset for the corrective-action lists: applied *on top of* an authorized queryset (``selectors.actions_visible_to`` etc.).

Relation choices are drawn from the same authorized scope, so a filter can never name - or reveal - something outside the caller's scope.
``status`` accepts the derived ``overdue`` as well as the persisted statuses; ``location`` includes descendants.
"""

from __future__ import annotations

import django_filters
from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.core.forms import DesignSystemFilterForm
from apps.findings.models import Finding
from apps.locations.models import Location

from . import selectors
from .constants import OVERDUE, ActionStatus, Decision, Priority
from .models import CorrectiveAction

STATUS_CHOICES = [*ActionStatus.choices, (OVERDUE, "Overdue")]
_DATE = django_filters.widgets.RangeWidget(attrs={"type": "date", "class": "input"})


def _scope(request):  # noqa: ANN001, ANN202
    return selectors.actions_visible_to(request.user) if request else CorrectiveAction.objects.none()


def _findings(request):  # noqa: ANN001, ANN202
    return Finding.objects.filter(pk__in=_scope(request).values("finding_id")).order_by("-number")


def _locations(request):  # noqa: ANN001, ANN202
    if not request:
        return Location.objects.none()
    from apps.permissions.authorization import get_accessible_locations
    from apps.permissions.registry import P

    return Location.objects.filter(Q(pk__in=_scope(request).values("location_id")) | Q(pk__in=get_accessible_locations(request.user, P.CORRECTIVE_ACTION_VIEW).values("pk"))).order_by("name")


def _owners(request):  # noqa: ANN001, ANN202
    return get_user_model().objects.filter(pk__in=_scope(request).values("assigned_to_id")).order_by("email")


class ActionFilter(django_filters.FilterSet):
    q = django_filters.CharFilter(method="filter_search", label="Search")
    status = django_filters.ChoiceFilter(choices=STATUS_CHOICES, method="filter_status", label="Status")
    priority = django_filters.ChoiceFilter(choices=Priority.choices, label="Priority")
    finding = django_filters.ModelChoiceFilter(queryset=_findings, label="Finding")
    location = django_filters.ModelChoiceFilter(queryset=_locations, method="filter_location", label="Location (with sub-locations)")
    assigned_to = django_filters.ModelChoiceFilter(queryset=_owners, label="Responsible")
    verification = django_filters.ChoiceFilter(choices=Decision.choices, method="filter_verification", label="Verification outcome")
    due = django_filters.DateFromToRangeFilter(field_name="due_at", label="Due between", widget=_DATE)
    created = django_filters.DateFromToRangeFilter(field_name="created_at", label="Created between", widget=_DATE)

    class Meta:
        model = CorrectiveAction
        form = DesignSystemFilterForm
        fields = ("q", "status", "priority", "finding", "location", "assigned_to", "verification", "due", "created")

    def filter_search(self, queryset, name, value):  # noqa: ANN001, ANN201
        digits = "".join(ch for ch in value if ch.isdigit())
        number = Q(number=int(digits)) if digits and (value.upper().startswith(("CA", "#")) or value.isdigit()) else Q()
        finding = Q(finding__number=int(digits)) if digits and value.upper().startswith("FND") else Q()
        return queryset.filter(number | finding | Q(title__icontains=value) | Q(finding__title__icontains=value) | Q(location__name__icontains=value))

    def filter_status(self, queryset, name, value):  # noqa: ANN001, ANN201
        return selectors.overdue(queryset) if value == OVERDUE else queryset.filter(status=value)

    def filter_location(self, queryset, name, value):  # noqa: ANN001, ANN201
        from apps.locations.selectors import get_descendants

        return queryset.filter(location__in=get_descendants(value, include_self=True).values("pk"))

    def filter_verification(self, queryset, name, value):  # noqa: ANN001, ANN201
        return queryset.filter(verifications__decision=value).distinct()
