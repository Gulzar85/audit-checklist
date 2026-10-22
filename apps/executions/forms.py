"""Parsing of the workspace form into ``AnswerInput`` objects, and the worklist filter.

The form carries, per rendered item: ``p__<id>`` (presence marker - only items that were on the page are touched), ``r__<id>``
(the response revision the client last saw), ``v__<id>`` (value; several for multiple choice), ``m__<id>__<row>`` (matrix
cells), ``c__<id>`` (comment) and ``na__<id>``.  Nothing here validates an answer; the services do.
"""

from __future__ import annotations

from typing import Any

import django_filters
from django import forms
from django.db.models import Q

from apps.core.forms import DesignSystemFilterForm, DesignSystemFormMixin
from apps.assignments.constants import AssignmentPriority

from .services.evaluation import ExecutionState
from .services.responses import AnswerInput
from .services.validation import is_media


def _revision(raw: str | None) -> int:
    try:
        return max(int(raw or 0), 0)
    except (TypeError, ValueError):
        return 0


def inputs_from_post(post: Any, state: ExecutionState) -> list[AnswerInput]:
    """One ``AnswerInput`` per item whose presence marker was posted; unknown ids are passed on (the service refuses them)."""
    inputs: list[AnswerInput] = []
    for key in post:
        if not key.startswith("p__"):
            continue
        item_id = key[3:]
        spec = state.item_for(item_id)
        value: Any
        if spec is None:
            value = post.get(f"v__{item_id}")
        elif is_media(spec.answer_type):
            continue  # no control is rendered for these (evidence storage arrives later); never trust a forged field
        elif spec.answer_type == "multiple_choice":
            value = post.getlist(f"v__{item_id}")
        elif spec.answer_type == "matrix":
            multiple = spec.config.get("mode", "single") == "multiple"
            value = {}
            for row in spec.config.get("rows", []):
                cell = post.getlist(f"m__{item_id}__{row['key']}") if multiple else post.get(f"m__{item_id}__{row['key']}")
                if cell:
                    value[row["key"]] = cell
        else:
            value = post.get(f"v__{item_id}")
            if isinstance(value, str) and value.strip() == "":
                value = None
        inputs.append(AnswerInput(item=item_id, value=value, not_applicable=bool(post.get(f"na__{item_id}")),
                                  comment=post.get(f"c__{item_id}"), revision=_revision(post.get(f"r__{item_id}"))))
    return inputs


class CancelForm(DesignSystemFormMixin, forms.Form):
    reason = forms.CharField(max_length=300, widget=forms.Textarea(attrs={"rows": 2}), label="Reason")


STATE_CHOICES = [("todo", "Not started"), ("in_progress", "In progress"), ("overdue", "Overdue")]


class MyWorkFilter(django_filters.FilterSet):
    q = django_filters.CharFilter(method="filter_search", label="Search")
    state = django_filters.ChoiceFilter(choices=STATE_CHOICES, method="filter_state", label="Status")
    priority = django_filters.ChoiceFilter(choices=AssignmentPriority.choices, label="Priority")

    class Meta:
        from apps.assignments.models import ChecklistAssignment

        model = ChecklistAssignment
        form = DesignSystemFilterForm
        fields = ("q", "state", "priority")

    def filter_search(self, queryset, name, value):  # noqa: ANN001, ANN201
        digits = "".join(ch for ch in value if ch.isdigit())
        number = Q(number=int(digits)) if digits and value.upper().startswith("ASG") else Q()
        return queryset.filter(number | Q(version__template__name__icontains=value) | Q(location__name__icontains=value)
                               | Q(location__code__icontains=value))

    def filter_state(self, queryset, name, value):  # noqa: ANN001, ANN201
        from django.utils import timezone

        if value == "todo":
            return queryset.filter(status="assigned")
        if value == "in_progress":
            return queryset.filter(status="in_progress")
        return queryset.filter(status__in=["assigned", "in_progress"], due_at__lt=timezone.now())
