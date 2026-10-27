"""Scheduling screens (``/scheduling/``): thin CBVs over ``apps.jobs.services`` and ``apps.jobs.selectors`` - the same code the API uses."""

from __future__ import annotations

import json
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.context import get_active_organization

from . import constants as k
from . import policies, registry, selectors, services
from .forms import ScheduleForm
from .models import ScheduleDefinition

PAGE_SIZE = 20


class JobsMixin(LoginRequiredMixin):
    """Anyone who may view scheduling somewhere; each action re-checks its own permission in the service."""

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.user.is_authenticated and not policies.can_view_anywhere(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def organization(self) -> Any:
        return get_active_organization(self.request)

    def flags(self) -> dict[str, bool]:
        org, user = self.organization(), self.request.user
        return {"can_manage": policies.can_manage(user, org) or getattr(user, "is_superuser", False), "can_run": policies.can_run(user, org) or getattr(user, "is_superuser", False),
                "is_platform_admin": bool(getattr(user, "is_superuser", False))}


class DashboardView(JobsMixin, TemplateView):
    template_name = "jobs/dashboard.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return {**super().get_context_data(**kwargs), **self.flags(), "d": selectors.dashboard(self.request.user)}


class ScheduleListView(JobsMixin, TemplateView):
    template_name = "jobs/schedule_list.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        params = self.request.GET
        qs = selectors.schedules_visible_to(self.request.user).order_by("name")
        q, status, task = params.get("q", "").strip(), params.get("status", ""), params.get("task", "")
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
        if status in k.ScheduleStatus.values:
            qs = qs.filter(status=status)
        if task in registry.REGISTRY:
            qs = qs.filter(task_type=task)
        page = Paginator(qs, PAGE_SIZE).get_page(params.get("page"))
        for row in page.object_list:
            row.task = registry.get(row.task_type)
            row.last_job = row.jobs.order_by("-scheduled_for").first()  # one indexed lookup per row on a 20-row page
        return {**super().get_context_data(**kwargs), **self.flags(), "page": page, "q": q, "status": status, "task": task, "statuses": k.ScheduleStatus.choices, "tasks": registry.REGISTRY.values()}


class ScheduleDetailView(JobsMixin, TemplateView):
    template_name = "jobs/schedule_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        schedule = selectors.get_schedule(self.request.user, self.kwargs["pk"])
        if schedule is None:
            raise Http404
        task = registry.get(schedule.task_type)
        return {**super().get_context_data(**kwargs), **self.flags(), "schedule": schedule, "task": task, "jobs": schedule.jobs.order_by("-scheduled_for")[:15],
                "can_trigger": bool(task and policies.can_trigger(self.request.user, task, schedule.organization)), "can_edit": bool(task and policies.can_schedule(self.request.user, task, schedule.organization))}


class ScheduleFormView(JobsMixin, View):
    template_name = "jobs/schedule_form.html"

    def schedule(self) -> ScheduleDefinition | None:
        if "pk" not in self.kwargs:
            return None
        found = selectors.get_schedule(self.request.user, self.kwargs["pk"])
        if found is None:
            raise Http404
        return found

    def form(self, schedule: ScheduleDefinition | None, data: Any = None) -> ScheduleForm:
        org = schedule.organization if schedule else self.organization()
        initial: dict[str, Any] = {}
        if schedule:
            from .recurrence import get_zone

            zone = get_zone(schedule.timezone)
            fmt = "%Y-%m-%dT%H:%M"
            initial = {"name": schedule.name, "description": schedule.description, "task_type": schedule.task_type, "schedule_type": schedule.schedule_type, "timezone": schedule.timezone,
                       "start_at": schedule.start_at.astimezone(zone).strftime(fmt), "end_at": schedule.end_at.astimezone(zone).strftime(fmt) if schedule.end_at else "",
                       "every_minutes": schedule.recurrence.get("every_minutes"), "time": schedule.recurrence.get("time", ""), "weekdays": schedule.recurrence.get("weekdays", []),
                       "day": schedule.recurrence.get("day"), "misfire_policy": schedule.misfire_policy, "max_attempts": schedule.max_attempts,
                       "payload": "" if not schedule.payload else json.dumps(schedule.payload)}
        return ScheduleForm(data, initial=initial, user=self.request.user, organization=org, editing=schedule is not None)

    def get(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        schedule = self.schedule()
        return render(request, self.template_name, {**self.flags(), "form": self.form(schedule), "schedule": schedule, "organization": self.organization()})

    def post(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        schedule = self.schedule()
        form = self.form(schedule, request.POST)
        if form.is_valid():
            org = schedule.organization if schedule else (None if form.cleaned_data.get("platform_wide") else self.organization())
            try:
                saved = services.save_schedule(request.user, org, form.service_data(), schedule=schedule, allow_timezone_change=bool(form.cleaned_data.get("allow_timezone_change")))
            except PermissionDenied:
                raise
            except services.JobError as exc:
                form.add_error(None, exc.message)
            else:
                messages.success(request, "Schedule saved.")
                return HttpResponseRedirect(reverse("jobs:schedule", args=[saved.pk]))
        return render(request, self.template_name, {**self.flags(), "form": form, "schedule": schedule, "organization": self.organization()}, status=400)


class ScheduleActionView(JobsMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: str, action: str) -> HttpResponse:
        schedule = selectors.get_schedule(request.user, pk)
        if schedule is None:
            raise Http404
        try:
            if action == "run":
                services.run_now(schedule, request.user, client_key=request.POST.get("client_key", ""))
                messages.success(request, "The task was queued; the runner will pick it up on its next pass.")
            else:
                services.set_status(schedule, request.user, action)
                messages.success(request, f"Schedule {action}d.")
        except services.JobError as exc:
            messages.error(request, exc.message)
        return HttpResponseRedirect(reverse("jobs:schedule", args=[schedule.pk]))


class JobListView(JobsMixin, TemplateView):
    template_name = "jobs/job_list.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        params = self.request.GET
        qs = selectors.jobs_visible_to(self.request.user).order_by("-scheduled_for")
        status, task, q = params.get("status", ""), params.get("task", ""), params.get("q", "").strip()
        if status in k.JobStatus.values:
            qs = qs.filter(status=status)
        if task in registry.REGISTRY:
            qs = qs.filter(task_type=task)
        if q:
            qs = qs.filter(Q(correlation_id__icontains=q) | Q(schedule__name__icontains=q))
        page = Paginator(qs, PAGE_SIZE).get_page(params.get("page"))
        return {**super().get_context_data(**kwargs), **self.flags(), "page": page, "status": status, "task": task, "q": q, "statuses": k.JobStatus.choices, "tasks": registry.REGISTRY.values()}


class JobDetailView(JobsMixin, TemplateView):
    template_name = "jobs/job_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        job = selectors.get_job(self.request.user, self.kwargs["pk"])
        if job is None:
            raise Http404
        return {**super().get_context_data(**kwargs), **self.flags(), "job": job, "executions": selectors.executions_of(job), "can_act": policies.can_run(self.request.user, job.organization),
                "cancellable": job.status in k.CANCELLABLE, "retryable": job.status == k.JobStatus.FAILED}


class JobActionView(JobsMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: str, action: str) -> HttpResponse:
        job = selectors.get_job(request.user, pk)
        call = {"cancel": services.cancel, "retry": services.retry}.get(action)
        if job is None or call is None:
            raise Http404
        try:
            call(job, request.user, request.POST.get("reason", ""))
        except services.JobError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, "Job cancelled." if action == "cancel" else "The job will run again on the next runner pass.")
        return HttpResponseRedirect(reverse("jobs:job", args=[job.pk]))


class DiagnosticsView(JobsMixin, TemplateView):
    """Platform-wide runner health: superusers only (it reveals other organizations' activity)."""

    template_name = "jobs/diagnostics.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.user.is_authenticated and not getattr(request.user, "is_superuser", False):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        from django.conf import settings

        return {**super().get_context_data(**kwargs), "d": selectors.diagnostics(), "config": {"backend": getattr(settings, "JOBS_EXECUTION_BACKEND", "poll"), "lease": getattr(settings, "JOBS_LEASE_SECONDS", 600),
                                                                                              "grace": getattr(settings, "JOBS_MISFIRE_GRACE_SECONDS", 300), "retention": getattr(settings, "JOBS_HISTORY_RETENTION_DAYS", 90),
                                                                                              "tasks": len(registry.REGISTRY)}}

