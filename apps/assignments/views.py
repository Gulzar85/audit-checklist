"""Class-based views for assignments: list, detail, create/edit a draft, lifecycle actions and bulk assignment.

Pattern (as everywhere): the object is fetched through the authorized selector (not visible -> 404), the action is authorized by
the services (visible but forbidden -> 403), and every write is a service call whose ``ValidationError`` becomes a message or a
form error.  Nothing here changes a status; only ``services.lifecycle`` does.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views import View
from django.views.generic import FormView, TemplateView
from django_filters.views import FilterView
from django_tables2 import SingleTableMixin

from apps.core.forms import add_validation_error
from apps.permissions.authorization import has_permission_anywhere
from apps.permissions.registry import P

from . import policies, selectors, services
from .constants import AssignmentStatus
from .filters import AssignmentFilter
from .forms import AssigneeForm, AssignmentForm, BulkForm, CancelForm, DueForm, PriorityForm
from .services import AssignmentConflict
from .tables import AssignmentTable


def _text(exc: ValidationError) -> str:
    return " ".join(exc.messages)


class AssignmentAccessMixin(LoginRequiredMixin):
    """Collection gate: the user may see assignments somewhere (scope), or has been designated on one."""

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated:
            user = request.user
            if not (has_permission_anywhere(user, P.ASSIGNMENT_VIEW) or selectors.assignments_visible_to(user).exists()):
                raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class AssignmentObjectMixin(AssignmentAccessMixin):
    @property
    def assignment(self):  # noqa: ANN201
        if not hasattr(self, "_assignment"):
            found = selectors.get_assignment(self.request.user, self.kwargs["pk"])
            if found is None:
                raise Http404
            self._assignment = found
        return self._assignment


# ---------------------------------------------------------------------------
class AssignmentListView(AssignmentAccessMixin, SingleTableMixin, FilterView):
    table_class = AssignmentTable
    filterset_class = AssignmentFilter
    template_name = "assignments/assignment_list.html"
    table_pagination = {"per_page": 20}

    def get_queryset(self):  # noqa: ANN201
        return selectors.assignments_visible_to(self.request.user)  # authorize first, then search/filter/sort/paginate

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context.update(
            summary=selectors.summary(selectors.assignments_visible_to(user)),
            can_create=has_permission_anywhere(user, P.ASSIGNMENT_CREATE), can_bulk=has_permission_anywhere(user, P.ASSIGNMENT_BULK_CREATE),
            hidden_filters=(),
        )
        return context


class AssignmentDetailView(AssignmentObjectMixin, TemplateView):
    template_name = "assignments/assignment_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        a, user = self.assignment, self.request.user
        can_details = policies.can_change_details(user, a)
        context.update(
            assignment=a, path=a.location.display_path, timeline=selectors.timeline(a),
            can_edit=policies.can_edit_draft(user, a), can_issue=policies.can_issue(user, a), can_cancel=policies.can_cancel(user, a),
            can_reopen=policies.can_reopen(user, a), can_details=can_details,
            can_view_checklist=policies.can_use_checklist(user, a.organization),
            assignees=selectors.eligible_assignees(a.organization, a.location) if can_details else [],
            priorities=a._meta.get_field("priority").choices,
        )
        return context


# ---------------------------------------------------------------------------
class AssignmentFormMixin:
    """Shared by create and edit: the form, the optional review step, the service call."""

    template_name = "assignments/assignment_form.html"
    form_class = AssignmentForm

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, assignment=getattr(self, "assignment", None))
        return kwargs

    def render_form(self, form: AssignmentForm) -> HttpResponse:
        return self.render_to_response(self.get_context_data(form=form))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = self.get_form()
        action = request.POST.get("action", "draft")
        if action == "edit" or not form.is_valid():
            return self.render_form(form)
        if action == "review":  # the confirmation summary shown before an assignment is issued
            data = form.cleaned_data
            return render(request, "assignments/assignment_confirm.html", {
                "form": form, "data": data, "path": data["location"].display_path, "assignment": getattr(self, "assignment", None),
                "assignee": data["assigned_to"], "version": data["version"],
            })
        try:
            assignment = self.save(form.cleaned_data, issue=action == "issue")
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.render_form(form)
        messages.success(request, f"{assignment.reference} {'issued' if assignment.status == AssignmentStatus.ASSIGNED else 'saved as a draft'}.")
        return HttpResponseRedirect(assignment.get_absolute_url())


class AssignmentCreateView(AssignmentFormMixin, AssignmentAccessMixin, FormView):
    assignment = None

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated and not has_permission_anywhere(request.user, P.ASSIGNMENT_CREATE):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):  # noqa: ANN201
        initial = {}
        location = self.request.GET.get("location")
        if location:
            initial["location"] = location
        return initial

    def save(self, data: dict[str, Any], *, issue: bool):  # noqa: ANN201
        return services.create_assignment(
            actor=self.request.user, version=data["version"], location=data["location"], priority=data["priority"], due_at=data["due_at"],
            instructions=data["instructions"], assigned_to=data["assigned_to"], issue=issue,
        )


class AssignmentUpdateView(AssignmentFormMixin, AssignmentObjectMixin, FormView):
    """Edit a draft.  An issued assignment is changed only through the dedicated actions (assignee, due date, priority)."""

    def get_initial(self):  # noqa: ANN201
        a = self.assignment
        if not policies.can_edit_draft(self.request.user, a):
            raise PermissionDenied
        return {"version": a.version, "location": a.location, "assigned_to": a.assigned_to, "due_at": a.due_at,
                "priority": a.priority, "instructions": a.instructions}

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not policies.can_edit_draft(request.user, self.assignment):
            raise PermissionDenied
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(assignment=self.assignment, **kwargs)

    def save(self, data: dict[str, Any], *, issue: bool):  # noqa: ANN201
        a = services.update_draft(self.assignment, actor=self.request.user, **data)
        return services.issue(a, actor=self.request.user) if issue else a


# ---------------------------------------------------------------------------
class AssignmentActionView(AssignmentObjectMixin, View):
    """POST-only lifecycle/detail actions: authorize in the service, flash the outcome, return to the detail page."""

    http_method_names = ["post"]
    success = "Saved."

    def perform(self, request: HttpRequest) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            self.perform(request)
        except AssignmentConflict as exc:  # a repeated or racing request: informational, nothing changed
            messages.info(request, _text(exc))
        except ValidationError as exc:
            messages.error(request, _text(exc))
        else:
            messages.success(request, self.success)
        return HttpResponseRedirect(self.assignment.get_absolute_url())


class AssignmentIssueView(AssignmentActionView):
    success = "Assignment issued."

    def perform(self, request: HttpRequest) -> Any:
        return services.issue(self.assignment, actor=request.user)


class AssignmentCancelView(AssignmentActionView):
    success = "Assignment cancelled."

    def perform(self, request: HttpRequest) -> Any:
        form = CancelForm(request.POST)
        if not form.is_valid():
            raise ValidationError(form.errors["reason"])
        return services.cancel(self.assignment, actor=request.user, reason=form.cleaned_data["reason"])


class AssignmentReopenView(AssignmentActionView):
    success = "Assignment reopened as a draft."

    def perform(self, request: HttpRequest) -> Any:
        return services.reopen(self.assignment, actor=request.user)


class AssignmentPriorityView(AssignmentActionView):
    success = "Priority changed."

    def perform(self, request: HttpRequest) -> Any:
        form = PriorityForm(request.POST)
        if not form.is_valid():
            raise ValidationError("Choose a valid priority.")
        return services.change_priority(self.assignment, actor=request.user, priority=form.cleaned_data["priority"])


class AssignmentDueDateView(AssignmentActionView):
    success = "Due date changed."

    def perform(self, request: HttpRequest) -> Any:
        form = DueForm(request.POST)
        if not form.is_valid():
            raise ValidationError("Enter a valid date and time.")
        return services.change_due_date(self.assignment, actor=request.user, due_at=form.cleaned_data["due_at"])


class AssignmentAssigneeView(AssignmentActionView):
    success = "Responsible user changed."

    def perform(self, request: HttpRequest) -> Any:
        form = AssigneeForm(request.POST)
        if not form.is_valid():
            raise ValidationError("Choose a valid user.")
        user = None
        if form.cleaned_data["assigned_to"]:
            user = selectors.eligible_assignees(self.assignment.organization, self.assignment.location).filter(pk=form.cleaned_data["assigned_to"]).first()
            if user is None:
                raise ValidationError("That user cannot be designated for this location.")
        return services.change_assignee(self.assignment, actor=request.user, assigned_to=user)


class AssigneeOptionsView(AssignmentAccessMixin, TemplateView):
    """HTMX: the users who may be designated at the chosen location (only for locations the caller may create at)."""

    template_name = "assignments/_assignee_options.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        raw = self.request.GET.get("location", "")
        location = selectors.assignable_locations(self.request.user).filter(pk=raw).first() if raw and len(raw) == 36 else None
        users = selectors.eligible_assignees(location.organization, location) if location else []
        return super().get_context_data(users=users, **kwargs)


# ---------------------------------------------------------------------------
class BulkAssignView(AssignmentAccessMixin, FormView):
    """Assign one published version to many locations: choose -> review (dry run) -> confirm (all-or-nothing)."""

    template_name = "assignments/bulk_form.html"
    form_class = BulkForm

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated and not has_permission_anywhere(request.user, P.ASSIGNMENT_BULK_CREATE):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def picker_rows(self, selected: set[str]) -> list[dict[str, Any]]:
        locations = list(selectors.assignable_locations(self.request.user, P.ASSIGNMENT_BULK_CREATE).order_by("name"))
        paths = selectors.location_paths([loc.organization_id for loc in locations])
        rows = [{"location": loc, "path": paths.get(loc.pk, loc.name), "checked": str(loc.pk) in selected} for loc in locations]
        return sorted(rows, key=lambda r: r["path"])

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        selected = set(self.request.POST.getlist("locations")) if self.request.method == "POST" else set()
        return super().get_context_data(rows=self.picker_rows(selected), **kwargs)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = self.get_form()
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        data, user = form.cleaned_data, request.user
        stage = request.POST.get("stage", "review")
        if stage == "review":
            roots = list(selectors.assignable_locations(user, P.ASSIGNMENT_BULK_CREATE).filter(pk__in=_uuids(request.POST.getlist("locations"))))
            if not roots:
                form.add_error(None, "Choose at least one location.")
                return self.render_to_response(self.get_context_data(form=form))
            targets, skipped = selectors.expand_targets(user, roots, include_descendants=data["include_descendants"])
            ids = [str(t.pk) for t in targets]
        else:  # confirm: the final list was fixed (and shown) in the review step
            ids, skipped = _uuids(request.POST.getlist("locations")), int(request.POST.get("skipped") or 0)
        try:
            result = services.bulk_create_assignments(
                actor=user, version=data["version"], location_ids=ids, priority=data["priority"], due_at=data["due_at"],
                instructions=data["instructions"], issue=data["issue"], dry_run=stage == "review",
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.render_to_response(self.get_context_data(form=form))
        if stage == "confirm" and result.created:
            messages.success(request, f"{result.count} assignment(s) created" + (" and issued." if data["issue"] else " as drafts."))
        elif stage == "confirm":
            messages.error(request, "Nothing was created: at least one location could not be assigned. Review the list below.")
        return render(request, "assignments/bulk_confirm.html", {
            "form": form, "data": data, "result": result, "stage": stage, "skipped": skipped, "ids": ids,
            "all_ok": all(r.ok for r in result.results), "created": stage == "confirm" and result.created,
        })


def _uuids(values: list[str]) -> list[str]:
    import uuid

    out = []
    for value in values:
        try:
            out.append(str(uuid.UUID(value)))
        except ValueError:
            continue
    return out

