"""Class-based views for workflows: the designer (definitions, versions, states, transitions, validation, publication) and the runtime (tasks, instances).

Pattern (as everywhere): objects come from authorized selectors (invisible -> 404), actions are authorized by the services (visible but forbidden -> 403) and every
write is a service call whose ``ValidationError`` becomes a message or a form error.  Nothing here changes a state: only ``services.engine`` does.  The runtime forms carry a
one-time idempotency key and the revision the user saw, so a double click or a stale page can never execute a decision twice.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.views import View
from django.views.generic import FormView, TemplateView

from apps.accounts.context import get_active_organization
from apps.core.forms import add_validation_error
from apps.permissions.authorization import has_permission_anywhere
from apps.permissions.registry import P

from . import assignment, conditions, policies, selectors, services, targets, validation
from .constants import InstanceStatus, VersionStatus
from .forms import ActionForm, DefinitionForm, ReasonForm, ReassignForm, StateForm, TransitionForm, VersionForm
from .models import WorkflowTask
from .services import WorkflowConflict


def _text(exc: ValidationError) -> str:
    return " ".join(exc.messages)


class DesignerMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated and not has_permission_anywhere(request.user, P.WORKFLOW_VIEW):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class DefinitionMixin(DesignerMixin):
    @property
    def definition(self):  # noqa: ANN201
        if not hasattr(self, "_definition"):
            found = selectors.get_definition(self.request.user, self.kwargs["pk"])
            if found is None:
                raise Http404
            self._definition = found
        return self._definition


class VersionMixin(DesignerMixin):
    @property
    def version(self):  # noqa: ANN201
        if not hasattr(self, "_version"):
            found = selectors.get_version(self.request.user, self.kwargs["pk"])
            if found is None:
                raise Http404
            self._version = found
        return self._version


# ---------------------------------------------------------------------------------------------------------------------------------
class WorkflowListView(DesignerMixin, TemplateView):
    template_name = "workflows/workflow_list.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        user = self.request.user
        rows = list(selectors.definitions_visible_to(user).prefetch_related("versions").order_by("organization", "name"))
        for d in rows:
            d.published = next((v for v in d.versions.all() if v.status == VersionStatus.PUBLISHED), None)
            d.draft = next((v for v in d.versions.all() if v.status == VersionStatus.DRAFT), None)
        organization = get_active_organization(self.request)
        return super().get_context_data(definitions=rows, can_create=organization is not None and policies.can_create(user, organization), summary=selectors.task_summary(user), **kwargs)


class WorkflowCreateView(DesignerMixin, FormView):
    template_name = "workflows/definition_form.html"
    form_class = DefinitionForm

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated:
            organization = get_active_organization(request)
            if organization is None or not policies.can_create(request.user, organization):
                raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):  # noqa: ANN001, ANN201
        d = form.cleaned_data
        try:
            definition = services.create_definition(actor=self.request.user, organization=get_active_organization(self.request), code=d["code"], name=d["name"],
                                                     target_type=d["target_type"], description=d["description"], is_default=d["is_default"], auto_start_event=d["auto_start_event"])
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, "Workflow created with an empty draft. Add states and transitions, then validate and publish.")
        return HttpResponseRedirect(definition.versions.get().get_absolute_url())


class WorkflowEditView(DefinitionMixin, FormView):
    template_name = "workflows/definition_form.html"
    form_class = DefinitionForm

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["editing"] = True
        return kwargs

    def get_initial(self):  # noqa: ANN201
        d = self.definition
        if not policies._design(self.request.user, d, "workflow.update_draft"):
            raise PermissionDenied
        return {"code": d.code, "name": d.name, "description": d.description, "target_type": d.target_type, "is_default": d.is_default, "auto_start_event": d.auto_start_event}

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(definition=self.definition, **kwargs)

    def form_valid(self, form):  # noqa: ANN001, ANN201
        d = form.cleaned_data
        try:
            services.update_definition(self.definition, actor=self.request.user, name=d["name"], description=d["description"], is_default=d["is_default"], auto_start_event=d["auto_start_event"])
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, "Saved.")
        return HttpResponseRedirect(self.definition.get_absolute_url())


class WorkflowDetailView(DefinitionMixin, TemplateView):
    template_name = "workflows/workflow_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        d, user = self.definition, self.request.user
        return super().get_context_data(definition=d, versions=d.versions.order_by("-number"), spec=targets.get_spec(d.target_type), can_version=policies.can_create_version(user, d),
                                        can_archive=policies.can_archive(user, d), can_edit=policies._design(user, d, "workflow.update_draft"),
                                        active_instances=d.instances.filter(status=InstanceStatus.ACTIVE).count(), **kwargs)


class _Post(DefinitionMixin, View):
    http_method_names = ["post"]

    def run(self, request: HttpRequest) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    success = "Done."

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            result = self.run(request)
        except WorkflowConflict as exc:
            messages.info(request, _text(exc))
            return HttpResponseRedirect(self.redirect(None))
        except ValidationError as exc:
            messages.error(request, _text(exc))
            return HttpResponseRedirect(self.redirect(None))
        messages.success(request, self.success)
        return HttpResponseRedirect(self.redirect(result))

    def redirect(self, result: Any) -> str:
        return self.definition.get_absolute_url()


class DraftCreateView(_Post):
    success = "A new draft version was created from the published one."

    def run(self, request: HttpRequest) -> Any:
        return services.create_draft(self.definition, actor=request.user)

    def redirect(self, result: Any) -> str:
        return result.get_absolute_url() if result is not None else self.definition.get_absolute_url()


class DefinitionArchiveView(_Post):
    success = "The workflow was archived. Running workflows keep their version."

    def run(self, request: HttpRequest) -> Any:
        return services.archive_definition(self.definition, actor=request.user)


# ---------------------------------------------------------------------------------------------------------------------------------
class VersionView(VersionMixin, TemplateView):
    """The structured editor: states, transitions, version settings, the validation report and a readable map of the workflow."""

    template_name = "workflows/version.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        v, user = self.version, self.request.user
        editable = policies.can_edit_draft(user, v)
        target_type = v.definition.target_type
        states = list(v.states.order_by("sort_order", "code"))
        transitions = list(v.transitions.select_related("source", "target").order_by("source__sort_order", "sort_order", "action"))
        for t in transitions:
            t.condition_text = conditions.describe(target_type, t.condition)
        report = validation.validate(v)
        edit_state = next((s for s in states if s.code == self.request.GET.get("edit_state")), None) if editable else None
        edit_transition = next((t for t in transitions if str(t.pk) == self.request.GET.get("edit_transition")), None) if editable else None
        context = dict(version=v, definition=v.definition, states=states, transitions=transitions, report=report, editable=editable, can_validate=policies.can_validate(user, v),
                       can_publish=policies.can_publish(user, v), can_clone=policies.can_create_version(user, v.definition), spec=targets.get_spec(target_type),
                       start_text=conditions.describe(target_type, v.start_condition), edit_state=edit_state, edit_transition=edit_transition,
                       map=[(s, [t for t in transitions if t.source_id == s.pk]) for s in states])
        if editable:
            context["version_form"] = kwargs.get("version_form") or VersionForm(initial={"change_summary": v.change_summary, "allow_loops": v.allow_loops,
                                                                                         "cond_json": _json(v.start_condition)}, target_type=target_type)
            context["state_form"] = kwargs.get("state_form") or StateForm(initial=_state_initial(edit_state) if edit_state else {"kind": "intermediate", "on_no_assignee": "block"},
                                                                          editing=edit_state is not None)
            context["transition_form"] = kwargs.get("transition_form") or TransitionForm(initial=_transition_initial(edit_transition) if edit_transition else {"actor_rule": "assignee"}, version=v)
        return super().get_context_data(**context)


def _json(value: Any) -> str:
    import json

    return json.dumps(value, indent=1) if value else ""


def _state_initial(s: Any) -> dict[str, Any]:
    policy = s.assignee_policy or {}
    return {"code": s.code, "name": s.name, "description": s.description, "kind": s.kind, "sort_order": s.sort_order, "assignee_type": policy.get("type", ""),
            "assignee_permission": policy.get("permission", ""), "assignee_role": policy.get("role", ""), "assignee_users": ", ".join(policy.get("users", [])),
            "approval_mode": s.approval_mode, "min_approvals": s.min_approvals, "separation": s.separation, "due_hours": s.due_hours, "on_no_assignee": s.on_no_assignee}


def _transition_initial(t: Any) -> dict[str, Any]:
    cond = t.condition if isinstance(t.condition, dict) else None
    simple = cond if cond and "attribute" in cond else None
    return {"source": t.source.code, "target": t.target.code, "action": t.action, "label": t.label, "decision": t.decision, "actor_rule": t.actor_rule, "handler": t.handler,
            "reason_required": t.reason_required, "sort_order": t.sort_order, "enabled": t.enabled, "cond_attribute": simple["attribute"] if simple else "",
            "cond_operator": simple["operator"] if simple else "", "cond_value": ", ".join(map(str, simple["value"])) if simple and isinstance(simple["value"], list) else
            ("" if not simple or simple["value"] is None else str(simple["value"])), "cond_json": "" if simple else _json(cond)}


class _VersionPost(VersionMixin, View):
    http_method_names = ["post"]
    success = "Saved."

    def run(self, request: HttpRequest) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def back(self, **query: str) -> HttpResponse:
        suffix = ("?" + "&".join(f"{k}={v}" for k, v in query.items())) if query else ""
        return HttpResponseRedirect(self.version.get_absolute_url() + suffix)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            self.run(request)
        except WorkflowConflict as exc:
            messages.info(request, _text(exc))
        except ValidationError as exc:
            messages.error(request, _text(exc))
        else:
            messages.success(request, self.success)
        return self.back()


class VersionSaveView(_VersionPost):
    def run(self, request: HttpRequest) -> Any:
        form = VersionForm(request.POST, target_type=self.version.definition.target_type)
        if not form.is_valid():
            raise ValidationError("Check the form.")
        condition, error = form.build_condition(self.version.definition.target_type)
        if error:
            raise ValidationError(error)
        return services.update_version(self.version, actor=request.user, change_summary=form.cleaned_data["change_summary"], start_condition=condition, allow_loops=form.cleaned_data["allow_loops"])


class StateSaveView(_VersionPost):
    def run(self, request: HttpRequest) -> Any:
        code = request.POST.get("code", "")
        existing = self.version.states.filter(code=code).first() if request.POST.get("editing") else None
        form = StateForm(request.POST, editing=existing is not None)
        if existing is not None:
            form.fields["code"].required = False
        if not form.is_valid():
            raise ValidationError(" ".join(e for errs in form.errors.values() for e in errs))
        d = form.cleaned_data
        values = dict(name=d["name"], description=d["description"], kind=d["kind"], assignee_policy=form.policy(), approval_mode=d["approval_mode"], min_approvals=d["min_approvals"] or 1,
                      separation=d["separation"], due_hours=d["due_hours"], on_no_assignee=d["on_no_assignee"])
        if d["sort_order"] is not None:
            values["sort_order"] = d["sort_order"]
        if existing is not None:
            return services.update_state(existing, actor=request.user, **values)
        return services.add_state(self.version, actor=request.user, code=d["code"], **values)


class StateDeleteView(_VersionPost):
    success = "State removed."

    def run(self, request: HttpRequest) -> Any:
        state = self.version.states.filter(code=request.POST.get("code", "")).first()
        if state is None:
            raise ValidationError("Unknown state.")
        return services.delete_state(state, actor=request.user)


class TransitionSaveView(_VersionPost):
    def run(self, request: HttpRequest) -> Any:
        form = TransitionForm(request.POST, version=self.version)
        if not form.is_valid():
            raise ValidationError(" ".join(e for errs in form.errors.values() for e in errs))
        d = form.cleaned_data
        condition, error = form.build_condition(self.version.definition.target_type)
        if error:
            raise ValidationError(error)
        values = dict(source=d["source"], target=d["target"], action=d["action"], label=d["label"], decision=d["decision"], actor_rule=d["actor_rule"], handler=d["handler"],
                      reason_required=d["reason_required"], enabled=d["enabled"], condition=condition)
        if d["sort_order"] is not None:
            values["sort_order"] = d["sort_order"]
        existing = self.version.transitions.filter(pk=request.POST["transition_id"]).first() if request.POST.get("transition_id") else None
        if existing is not None:
            return services.update_transition(existing, actor=request.user, **values)
        return services.add_transition(self.version, actor=request.user, **values)


class TransitionDeleteView(_VersionPost):
    success = "Transition removed."

    def run(self, request: HttpRequest) -> Any:
        t = self.version.transitions.filter(pk=request.POST.get("transition_id", "").strip() or None).first()
        if t is None:
            raise ValidationError("Unknown transition.")
        return services.delete_transition(t, actor=request.user)


class VersionValidateView(_VersionPost):
    success = "Validated. See the report below."

    def run(self, request: HttpRequest) -> Any:
        return services.validate_version(self.version, actor=request.user)


class VersionPublishView(_VersionPost):
    success = "Published. New workflows start on this version; running ones keep theirs."

    def run(self, request: HttpRequest) -> Any:
        try:
            return services.publish(self.version, actor=request.user)
        except services.WorkflowValidationError as exc:
            raise ValidationError("It cannot be published yet: " + " ".join(exc.report.errors[:5])) from exc

    def back(self, **query: str) -> HttpResponse:
        return HttpResponseRedirect(self.version.definition.get_absolute_url())


class VersionCloneView(_VersionPost):
    success = "A new draft was created from this version."

    def run(self, request: HttpRequest) -> Any:
        self._new = services.create_draft(self.version.definition, actor=request.user, clone_from=self.version)
        return self._new

    def back(self, **query: str) -> HttpResponse:
        return HttpResponseRedirect(getattr(self, "_new", self.version).get_absolute_url())


class VersionArchiveView(_VersionPost):
    success = "Version archived."

    def run(self, request: HttpRequest) -> Any:
        return services.archive_version(self.version, actor=request.user)

    def back(self, **query: str) -> HttpResponse:
        return HttpResponseRedirect(self.version.definition.get_absolute_url())


# ---------------------------------------------------------------------------------------------------------------------------------
# Runtime
class RuntimeMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        user = request.user
        if user.is_authenticated and not (has_permission_anywhere(user, P.WORKFLOW_TASK_VIEW) or has_permission_anywhere(user, P.WORKFLOW_INSTANCE_VIEW)
                                          or selectors.tasks_visible_to(user).exists() or selectors.instances_visible_to(user).exists()):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class InstanceMixin(RuntimeMixin):
    @property
    def instance(self):  # noqa: ANN201
        if not hasattr(self, "_instance"):
            found = selectors.get_instance(self.request.user, self.kwargs["pk"])
            if found is None:
                raise Http404
            self._instance = found
        return self._instance


class TaskListView(RuntimeMixin, TemplateView):
    template_name = "workflows/task_list.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        user = self.request.user
        mine = list(selectors.my_tasks(user).order_by("due_at", "created_at"))
        mine_ids = {t.pk for t in mine}
        others = [t for t in selectors.tasks_visible_to(user).filter(status__in=["pending", "claimed"], instance__status="active").order_by("due_at")[:50] if t.pk not in mine_ids]
        done = selectors.tasks_visible_to(user).filter(status="completed").order_by("-completed_at")[:10]
        return super().get_context_data(mine=mine, claimable=selectors.claimable_for(user), others=others, done=done, summary=selectors.task_summary(user), **kwargs)


class InstanceListView(RuntimeMixin, TemplateView):
    template_name = "workflows/instance_list.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        qs = selectors.instances_visible_to(self.request.user)
        status = self.request.GET.get("status", "")
        if status in InstanceStatus.values:
            qs = qs.filter(status=status)
        return super().get_context_data(instances=qs.order_by("-started_at")[:100], status=status, statuses=InstanceStatus.choices, **kwargs)


class InstanceDetailView(InstanceMixin, TemplateView):
    template_name = "workflows/instance_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        i, user = self.instance, self.request.user
        spec = targets.get_spec(i.target_type)
        obj = spec.resolve_system(i.target_id)
        visible = bool(obj is not None and spec.can_view(user, obj))  # the task never grants access to the record itself
        tasks = list(i.tasks.filter(entry_seq=i.entry_seq).select_related("state", "assignee"))
        claimable = {t.pk for t in selectors.claimable_for(user)} if i.is_active else set()
        for t in tasks:
            t.can_claim = t.pk in claimable
            t.can_release = t.assignee_id is not None and t.slot == "group" and (t.assignee_id == user.pk or policies.can_reassign_at(user, i.location)) and t.is_open
            t.candidates = [] if not (policies.can_reassign_at(user, i.location) and t.is_open and i.state.assignee_policy) else assignment.candidates(i.state.assignee_policy, i.organization, i.location)
        return super().get_context_data(
            instance=i, target=obj if visible else None, target_label=spec.label, target_reference=spec.reference(obj) if visible else "", target_title=spec.title(obj) if visible else "",
            target_url=spec.url(obj) if visible else "", available=services.available_transitions(i, user) if i.is_active else [], tasks=tasks, history=selectors.history(i),
            timeline=selectors.timeline(i), can_cancel=policies.can_cancel(user, i), key=uuid.uuid4(), **kwargs)


class _InstancePost(InstanceMixin, View):
    http_method_names = ["post"]
    success = "Done."

    def run(self, request: HttpRequest) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            self.message = self.run(request) or self.success
        except WorkflowConflict as exc:
            messages.info(request, _text(exc))
        except ValidationError as exc:
            messages.error(request, _text(exc))
        else:
            messages.success(request, self.message)
        return HttpResponseRedirect(self.instance.get_absolute_url())


class InstanceTransitionView(_InstancePost):
    def run(self, request: HttpRequest) -> Any:
        form = ActionForm(request.POST)
        if not form.is_valid():
            raise ValidationError("Choose an action.")
        d = form.cleaned_data
        result = services.perform(self.instance, d["action"], actor=request.user, comment=d["comment"], idempotency_key=d["idempotency_key"], expected_revision=d["expected_revision"])
        if result.replayed:
            return "That decision was already recorded; nothing was repeated."
        if result.pending_approvals:
            return f"Your approval was recorded. {result.pending_approvals} more approval(s) are needed."
        return "The decision was recorded and the business action completed."


class InstanceCancelView(_InstancePost):
    success = "The workflow was cancelled. The business record was not changed."

    def run(self, request: HttpRequest) -> Any:
        form = ReasonForm(request.POST)
        if not form.is_valid():
            raise ValidationError("A reason is required.")
        return services.cancel(self.instance, actor=request.user, reason=form.cleaned_data["reason"]) and None


class _TaskPost(RuntimeMixin, View):
    http_method_names = ["post"]

    def task(self, request: HttpRequest) -> WorkflowTask:
        found = selectors.tasks_visible_to(request.user).filter(pk=self.kwargs["pk"]).first()
        found = found or next((t for t in selectors.claimable_for(request.user) if str(t.pk) == str(self.kwargs["pk"])), None)
        if found is None:
            raise Http404
        return found

    def run(self, request: HttpRequest, task: WorkflowTask) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        task = self.task(request)
        try:
            text = self.run(request, task)
        except WorkflowConflict as exc:
            messages.info(request, _text(exc))
        except ValidationError as exc:
            messages.error(request, _text(exc))
        else:
            messages.success(request, text)
        return HttpResponseRedirect(task.instance.get_absolute_url())


class TaskClaimView(_TaskPost):
    def run(self, request: HttpRequest, task: WorkflowTask) -> str:
        services.claim(task, actor=request.user)
        return "The task is yours."


class TaskReleaseView(_TaskPost):
    def run(self, request: HttpRequest, task: WorkflowTask) -> str:
        services.release(task, actor=request.user)
        return "The task was released."


class TaskReassignView(_TaskPost):
    def run(self, request: HttpRequest, task: WorkflowTask) -> str:
        from django.contrib.auth import get_user_model

        form = ReassignForm(request.POST)
        if not form.is_valid():
            raise ValidationError("Choose a user.")
        user = get_user_model().objects.filter(pk=form.cleaned_data["assignee"]).first()
        if user is None:
            raise ValidationError("Unknown user.")
        services.reassign(task, actor=request.user, assignee=user)
        return "The task was reassigned."


class StartView(LoginRequiredMixin, View):
    """POST ``target_type`` and ``target_id``: start the default workflow for a record the user may see (the registry resolves it)."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest) -> HttpResponse:
        try:
            instance = services.start(actor=request.user, target_type=request.POST.get("target_type", ""), target=request.POST.get("target_id", ""),
                                      definition_code=request.POST.get("workflow") or None)
        except WorkflowConflict as exc:
            messages.info(request, _text(exc))
            return HttpResponseRedirect(request.META.get("HTTP_REFERER") or reverse("workflows:tasks"))
        except ValidationError as exc:
            if getattr(exc, "workflow_code", "") == "target_not_found":
                raise Http404 from exc
            messages.error(request, _text(exc))
            return HttpResponseRedirect(request.META.get("HTTP_REFERER") or reverse("workflows:tasks"))
        messages.success(request, "The workflow was started.")
        return HttpResponseRedirect(instance.get_absolute_url())
