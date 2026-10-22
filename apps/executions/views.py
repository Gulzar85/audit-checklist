"""Class-based views for executions: the worklist, start/resume, the workspace, review, submit, the read-only detail and history.

Pattern (as everywhere): an object is fetched through the authorized selector (not visible -> 404), the action is authorized by
the services (visible but forbidden -> 403), and every write is a service call whose ``ValidationError`` becomes a message.
Nothing here validates an answer, evaluates a rule or changes a status; the services do.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView
from django_filters.views import FilterView
from django_tables2 import SingleTableMixin

from apps.assignments import selectors as assignment_selectors
from apps.checklists.selectors import get_sections_for_version
from apps.permissions.authorization import has_permission_anywhere
from apps.permissions.registry import P

from . import policies, selectors, services
from .constants import LIVE_STATUSES, ExecutionStatus
from .display import format_answer
from .forms import CancelForm, MyWorkFilter, inputs_from_post
from .models import ChecklistExecution
from .presentation import build_sections
from .services.progress import progress_for_executions
from .tables import ExecutionTable, MyWorkTable


def _text(exc: ValidationError) -> str:
    return " ".join(exc.messages)


def _subtitle(ex: ChecklistExecution) -> str:
    return f"{ex.assignment.location.display_path} · version {ex.version.version_number} · {ex.assignment.reference}"


class WorkAccessMixin(LoginRequiredMixin):
    """Collection gate for the worklist: the user holds ``execution.create`` somewhere."""

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated and not has_permission_anywhere(request.user, P.EXECUTION_CREATE):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class ExecutionObjectMixin(LoginRequiredMixin):
    @property
    def execution(self) -> ChecklistExecution:
        if not hasattr(self, "_execution"):
            found = selectors.get_execution(self.request.user, self.kwargs["pk"])
            if found is None:
                raise Http404
            self._execution = found
        return self._execution


# ---------------------------------------------------------------------------
class MyWorkView(WorkAccessMixin, SingleTableMixin, FilterView):
    table_class = MyWorkTable
    filterset_class = MyWorkFilter
    template_name = "executions/my_work.html"
    table_pagination = {"per_page": 20}

    def get_queryset(self):  # noqa: ANN201
        from django.db.models import Prefetch

        live = Prefetch("executions", queryset=ChecklistExecution.objects.filter(status__in=LIVE_STATUSES).select_related("version"),
                        to_attr="live_executions")
        return selectors.my_assignments(self.request.user).prefetch_related(live).order_by("due_at", "-created_at")

    def get_table(self, **kwargs: Any):  # noqa: ANN401, ANN201
        table = super().get_table(**kwargs)
        rows = [row.record for row in table.page.object_list] if getattr(table, "page", None) else []
        live = [r.live_executions[0] for r in rows if r.live_executions]
        percents = progress_for_executions(live) if live else {}
        paths = assignment_selectors.location_paths({r.organization_id for r in rows}) if rows else {}
        for r in rows:
            r.live = r.live_executions[0] if r.live_executions else None
            r.progress = percents.get(r.live.pk) if r.live else None
            r.path = paths.get(r.location_id, r.location.name)
        return table

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        mine = selectors.my_assignments(self.request.user)
        from apps.assignments.selectors import overdue

        context.update(total=mine.count(), overdue=overdue(mine).count(), in_progress=mine.filter(status="in_progress").count(), hidden_filters=())
        return context


class StartExecutionView(WorkAccessMixin, View):
    """POST: start the assignment's execution, or resume it (idempotent); then open the workspace."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: Any) -> HttpResponse:
        assignment = selectors.assignment_for_work(request.user, pk)
        if assignment is None:  # not open to this user: indistinguishable from "does not exist"
            raise Http404
        try:
            execution, _created = services.start_or_resume(assignment, actor=request.user)
        except ValidationError as exc:
            messages.error(request, _text(exc))
            return HttpResponseRedirect(reverse("executions:list"))
        return HttpResponseRedirect(reverse("executions:workspace" if execution.status == ExecutionStatus.IN_PROGRESS else "executions:detail",
                                            args=[execution.pk]))


# ---------------------------------------------------------------------------
class WorkspaceView(ExecutionObjectMixin, TemplateView):
    """The checklist workspace: one section at a time, server-rendered from the evaluation of the stored answers."""

    template_name = "executions/workspace.html"

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated:
            ex = self.execution
            if ex.status != ExecutionStatus.IN_PROGRESS or not policies.can_work(request.user, ex):
                return HttpResponseRedirect(reverse("executions:detail", args=[ex.pk]))
        return super().dispatch(request, *args, **kwargs)

    # -- rendering --------------------------------------------------------------------------------------------------
    def context(self, *, posted=None, errors=None, conflicts=None, section=None, saved_at=None):  # noqa: ANN001, ANN201
        ex = self.execution
        state = services.build_state(ex)
        sections = build_sections(state, get_sections_for_version(ex.version), posted=posted, errors=errors, conflicts=conflicts,
                                  evidence_editable=policies.can_work(self.request.user, ex))
        visible = [s for s in sections if s.visible]
        current = next((s for s in visible if s.code == (section or self.request.GET.get("section"))), visible[0] if visible else None)
        index = visible.index(current) if current in visible else -1
        return {
            "execution": ex, "assignment": ex.assignment, "path": ex.assignment.location.display_path, "sections": visible,
            "subtitle": _subtitle(ex), "current": current,
            "previous_section": visible[index - 1] if index > 0 else None,
            "next_section": visible[index + 1] if 0 <= index < len(visible) - 1 else None,
            "progress": services.compute_progress(state), "saved_at": saved_at, "can_submit": policies.can_submit(self.request.user, ex),
            "can_cancel": policies.can_cancel(self.request.user, ex), "messages_for_form": state.evaluation.messages,
            "tampered": False,
        }

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return {**super().get_context_data(**kwargs), **self.context()}

    # -- saving -----------------------------------------------------------------------------------------------------
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ex = self.execution
        action = request.POST.get("action", "save")
        section = request.POST.get("section") or None
        htmx = request.headers.get("HX-Request") == "true"
        state = services.build_state(ex)
        try:
            result = services.save_answers(ex, actor=request.user, inputs=inputs_from_post(request.POST, state))
        except ValidationError as exc:  # already submitted / cancelled meanwhile
            messages.error(request, _text(exc))
            return HttpResponseRedirect(reverse("executions:detail", args=[ex.pk]))
        errors = {k: [p["message"] for p in v] for k, v in result.errors.items()}
        posted = {i.item: i.value for i in inputs_from_post(request.POST, state) if str(i.item) in errors}
        conflicts = set(result.conflicts)
        clean = not errors and not conflicts
        self._execution = result.execution
        if htmx or not clean:
            context = self.context(posted=posted, errors=errors, conflicts=conflicts, section=section,
                                   saved_at=timezone.localtime() if clean else None)
            if conflicts:
                messages.warning(request, "Some answers were changed elsewhere; the saved version is shown.") if not htmx else None
            template = "executions/_main.html" if htmx else self.template_name
            return render(request, template, {**context, "fragment": htmx}, status=200)
        if action == "review":
            return HttpResponseRedirect(reverse("executions:review", args=[ex.pk]))
        if action in ("next", "prev"):
            ctx = self.context(section=section)
            target = ctx["next_section"] if action == "next" else ctx["previous_section"]
            if target is not None:
                return HttpResponseRedirect(f"{reverse('executions:workspace', args=[ex.pk])}?section={target.code}")
        messages.success(request, "Draft saved.")
        suffix = f"?section={section}" if section else ""
        return HttpResponseRedirect(reverse("executions:workspace", args=[ex.pk]) + suffix)


class ReviewView(ExecutionObjectMixin, TemplateView):
    """The pre-submission review, evaluated on the server from the saved answers."""

    template_name = "executions/review.html"

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated:
            ex = self.execution
            if ex.status != ExecutionStatus.IN_PROGRESS or not policies.can_work(request.user, ex):
                return HttpResponseRedirect(reverse("executions:detail", args=[ex.pk]))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        ex = self.execution
        state, problems = services.review(ex)
        progress = services.compute_progress(state)
        context.update(
            execution=ex, assignment=ex.assignment, path=ex.assignment.location.display_path, progress=progress,
            problems=services.describe(problems, state), blocked=bool(problems), can_submit=policies.can_submit(self.request.user, ex),
            can_cancel=policies.can_cancel(self.request.user, ex), errors=kwargs.get("errors"),
        )
        return context


class SubmitView(ExecutionObjectMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: Any) -> HttpResponse:
        ex = self.execution
        try:
            result = services.submit(ex, actor=request.user)
        except services.ExecutionValidationError:
            messages.error(request, "The checklist cannot be submitted yet. Fix the problems below.")
            return HttpResponseRedirect(reverse("executions:review", args=[ex.pk]))
        except ValidationError as exc:
            messages.error(request, _text(exc))
            return HttpResponseRedirect(reverse("executions:detail", args=[ex.pk]))
        if result.already_submitted:
            messages.info(request, f"{ex.reference} was already submitted; nothing was changed.")
        else:
            messages.success(request, f"{ex.reference} submitted.")
        return HttpResponseRedirect(reverse("executions:detail", args=[ex.pk]))


class CancelExecutionView(ExecutionObjectMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: Any) -> HttpResponse:
        ex = self.execution
        form = CancelForm(request.POST)
        if not form.is_valid():
            messages.error(request, "A reason is required.")
            return HttpResponseRedirect(reverse("executions:detail", args=[ex.pk]))
        try:
            services.cancel_execution(ex, actor=request.user, reason=form.cleaned_data["reason"])
        except ValidationError as exc:
            messages.error(request, _text(exc))
        else:
            messages.success(request, f"{ex.reference} cancelled. Its answers are kept for the record.")
        return HttpResponseRedirect(reverse("executions:detail", args=[ex.pk]))


class ExecutionDetailView(ExecutionObjectMixin, TemplateView):
    """Read-only: the answers as saved (hidden-by-rules answers excluded), the status and the history."""

    template_name = "executions/detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        ex, user = self.execution, self.request.user
        state = services.build_state(ex)
        blocks = []
        for section in get_sections_for_version(ex.version):
            rows = []
            for item in section.items.all():
                result = state.evaluation.items.get(item.code)
                if result is None or not result.visible:
                    continue
                response = state.responses.get(item.code)
                if response is not None and response.is_hidden:
                    continue
                panel = None
                if item.answer_type in ("photo", "file", "signature") or result.evidence:
                    from apps.evidence.presentation import panel_for

                    panel = panel_for(ex, state.snapshot.items[item.code], result, state.evidence.get(str(item.pk), []), editable=False)
                rows.append({"item": item, "evidence": panel, "answer": format_answer(item, response), "comment": response.comment if response else "",
                             "required": result.required, "revision": response.revision if response else 0})
            if rows:
                blocks.append({"section": section, "rows": rows})
        context.update(
            execution=ex, assignment=ex.assignment, path=ex.assignment.location.display_path, blocks=blocks, subtitle=_subtitle(ex),
            progress=services.compute_progress(state), timeline=selectors.timeline(ex),
            can_continue=ex.status == ExecutionStatus.IN_PROGRESS and policies.can_work(user, ex),
            can_cancel=policies.can_cancel(user, ex),
            can_preview=has_permission_anywhere(user, P.CHECKLIST_VIEW),
        )
        return context


class HistoryView(LoginRequiredMixin, SingleTableMixin, TemplateView):
    """Executions the user may see (scope + their own): a status/history list."""

    template_name = "executions/history.html"
    table_class = ExecutionTable
    table_pagination = {"per_page": 20}

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated and not selectors.executions_visible_to(request.user).exists() and not has_permission_anywhere(request.user, P.EXECUTION_VIEW):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_table_data(self):  # noqa: ANN201
        qs = selectors.executions_visible_to(self.request.user)
        status = self.request.GET.get("status")
        if status in dict(ExecutionStatus.choices):
            qs = qs.filter(status=status)
        return qs.order_by("-started_at")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = ExecutionStatus.choices
        context["current_status"] = self.request.GET.get("status", "")
        return context



