"""Class-based views for corrective actions: lists (all, mine, verification queue), detail, create/edit, work log, lifecycle actions, evidence.

Pattern (as everywhere): the object is fetched through the authorized selector (not visible -> 404), the action is authorized by the
services (visible but forbidden -> 403), and every write is a service call whose ``ValidationError`` becomes a message or a form error.
Nothing here changes a status; only ``services.lifecycle`` does.  Evidence goes through the Phase 11 services and is served only after
re-checking the action and the attachment's belonging to it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.views import View
from django.views.generic import FormView, TemplateView
from django_filters.views import FilterView
from django_tables2 import SingleTableMixin

from apps.core.forms import add_validation_error
from apps.permissions.authorization import has_permission_anywhere
from apps.permissions.registry import P

from . import eligibility, policies, selectors, services
from .constants import ActionStatus, Purpose
from .filters import ActionFilter
from .forms import ActionForm, AssignForm, EditForm, EvidenceForm, ReasonForm, UpdateForm
from .services import ActionConflict
from .tables import ActionTable


def _text(exc: ValidationError) -> str:
    return " ".join(exc.messages)


class ActionAccessMixin(LoginRequiredMixin):
    """Collection gate: the user may see corrective actions somewhere (scope), or is responsible for one."""

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated:
            user = request.user
            if not (has_permission_anywhere(user, P.CORRECTIVE_ACTION_VIEW) or selectors.actions_visible_to(user).exists()):
                raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class ActionObjectMixin(ActionAccessMixin):
    @property
    def action(self):  # noqa: ANN201
        if not hasattr(self, "_action"):
            found = selectors.get_action(self.request.user, self.kwargs["pk"])
            if found is None:
                raise Http404
            self._action = found
        return self._action


# ---------------------------------------------------------------------------
class ActionListView(ActionAccessMixin, SingleTableMixin, FilterView):
    table_class = ActionTable
    filterset_class = ActionFilter
    template_name = "corrective_actions/action_list.html"
    table_pagination = {"per_page": 20}
    mode = "all"
    heading, subtitle, icon = "Corrective actions", "Work that addresses findings. Completing work, verifying it and closing it are separate steps.", "wrench"

    def get_queryset(self):  # noqa: ANN201
        return selectors.actions_visible_to(self.request.user)  # authorize first, then search/filter/sort/paginate

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context.update(summary=selectors.summary(self.summary_queryset()), mode=self.mode, heading=self.heading, subtitle=self.subtitle, icon=self.icon,
                       can_create=has_permission_anywhere(user, P.CORRECTIVE_ACTION_CREATE), hidden_filters=(),
                       can_verify_queue=has_permission_anywhere(user, P.CORRECTIVE_ACTION_VERIFY))
        return context

    def summary_queryset(self):  # noqa: ANN201
        return selectors.actions_visible_to(self.request.user)


class MyActionsView(ActionListView):
    """The user's own worklist: what is new, in progress, blocked, rejected, awaiting verification or overdue."""

    mode = "mine"
    heading, subtitle, icon = "My corrective actions", "Actions assigned to you. Rejected and overdue ones need your attention first.", "list-checks"

    def get_queryset(self):  # noqa: ANN201
        return selectors.my_actions(self.request.user)

    def summary_queryset(self):  # noqa: ANN201
        return selectors.my_actions(self.request.user)


class VerificationQueueView(ActionListView):
    """Submitted actions the user may decide (separation of duties applied)."""

    mode = "queue"
    heading, subtitle, icon = "Verification queue", "Submitted work waiting for an independent decision.", "badge-check"

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated and not has_permission_anywhere(request.user, P.CORRECTIVE_ACTION_VERIFY):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):  # noqa: ANN201
        return selectors.verification_queue(self.request.user)

    def summary_queryset(self):  # noqa: ANN201
        return selectors.verification_queue(self.request.user)


class ActionDetailView(ActionObjectMixin, TemplateView):
    template_name = "corrective_actions/action_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        a, user = self.action, self.request.user
        from apps.evidence.selectors import is_servable

        can_assign = policies.can_assign(user, a)
        evidence = selectors.evidence_for(a)
        grouped = {p.value: [] for p in Purpose}
        for att in evidence:
            grouped.setdefault(att.purpose, []).append({"attachment": att, "servable": is_servable(att.asset), "image": att.asset.is_image})
        context.update(
            action=a, path=a.location.display_path, timeline=selectors.timeline(a), actions=policies.available_actions(user, a),
            updates=list(a.updates.select_related("author", "corrects").order_by("-created_at")),
            verifications=list(a.verifications.select_related("verifier").order_by("-decided_at")),
            can_edit=policies.can_edit(user, a), can_manage=policies.can_manage(user, a), can_assign=can_assign, can_add_update=policies.can_add_update(user, a),
            assignees=selectors.eligible_assignees(a.organization, a.location) if can_assign else [],
            update_form=UpdateForm(action=a) if policies.can_add_update(user, a) else None,
            evidence_groups=[(p.label, p.value, grouped.get(p.value, []), policies.can_attach_evidence(user, a, p.value)) for p in Purpose],
            verified_files=selectors.verified_completion_count(a), eligibility=eligibility.finding_eligibility(a.finding),
            finding_url=self._finding_url(a), can_decide=policies.separation_ok(user, a) or not a.independent_verification,
        )
        return context

    def _finding_url(self, action) -> str:  # noqa: ANN001
        from apps.findings import selectors as finding_selectors

        found = finding_selectors.get_finding(self.request.user, action.finding_id)  # link only to a finding the user may open
        return found.get_absolute_url() if found else ""


# ---------------------------------------------------------------------------
class ActionCreateView(ActionAccessMixin, FormView):
    template_name = "corrective_actions/action_form.html"
    form_class = ActionForm

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated and not has_permission_anywhere(request.user, P.CORRECTIVE_ACTION_CREATE):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_initial(self):  # noqa: ANN201
        return {"finding": self.request.GET["finding"]} if self.request.GET.get("finding") else {}

    def form_valid(self, form: ActionForm) -> HttpResponse:
        d = form.cleaned_data
        try:
            action = services.create_action(
                actor=self.request.user, finding=d["finding"], title=d["title"], description=d["description"], plan=d["plan"], expected_outcome=d["expected_outcome"],
                completion_criteria=d["completion_criteria"], priority=d["priority"], assigned_to=d["assigned_to"], due_at=d["due_at"],
                required_evidence=d["required_evidence"], issue="issue" in self.request.POST)
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, f"{action.reference} was {'issued' if action.status != ActionStatus.DRAFT else 'saved as a draft'}.")
        return HttpResponseRedirect(action.get_absolute_url())


class ActionUpdateView(ActionObjectMixin, FormView):
    template_name = "corrective_actions/action_form.html"
    form_class = EditForm

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["manager"] = policies.can_manage(self.request.user, self.action)
        return kwargs

    def get_initial(self):  # noqa: ANN201
        a = self.action
        if not policies.can_edit(self.request.user, a):
            raise PermissionDenied
        return {"title": a.title, "description": a.description, "plan": a.plan, "expected_outcome": a.expected_outcome, "completion_criteria": a.completion_criteria,
                "priority": a.priority, "due_at": a.due_at, "required_evidence": a.required_evidence}

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not policies.can_edit(request.user, self.action):
            raise PermissionDenied
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(action=self.action, **kwargs)

    def form_valid(self, form: EditForm) -> HttpResponse:
        try:
            services.update_action(self.action, actor=self.request.user, **form.cleaned_data)
        except ActionConflict as exc:
            messages.info(self.request, _text(exc))
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        else:
            messages.success(self.request, "Changes saved.")
        return HttpResponseRedirect(self.action.get_absolute_url())


# ---------------------------------------------------------------------------
class ActionTransitionView(ActionObjectMixin, View):
    """POST-only lifecycle action: authorize in the service, flash the outcome, return to the detail page."""

    http_method_names = ["post"]
    transition = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = ReasonForm(request.POST)
        reason = form.cleaned_data["reason"] if form.is_valid() else ""
        try:
            action = services.transition(self.action, self.transition, actor=request.user, reason=reason)
        except ActionConflict as exc:  # a repeated or racing request: informational, nothing changed
            messages.info(request, _text(exc))
        except ValidationError as exc:
            messages.error(request, _text(exc))
        else:
            messages.success(request, f"{action.reference} is now {action.get_status_display().lower()}.")
        return HttpResponseRedirect(self.action.get_absolute_url())


class ActionAssignView(ActionObjectMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = AssignForm(request.POST)
        try:
            if not form.is_valid():
                raise ValidationError("Choose a valid user.")
            user = None
            if form.cleaned_data["assigned_to"]:
                user = selectors.eligible_assignees(self.action.organization, self.action.location).filter(pk=form.cleaned_data["assigned_to"]).first()
                if user is None:
                    raise ValidationError("That user cannot be made responsible for this action.")
            services.assign(self.action, actor=request.user, assigned_to=user)
        except ValidationError as exc:
            messages.error(request, _text(exc))
        else:
            messages.success(request, "Responsibility updated.")
        return HttpResponseRedirect(self.action.get_absolute_url())


class ActionProgressView(ActionObjectMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = UpdateForm(request.POST, action=self.action)
        if not form.is_valid():
            messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
            return HttpResponseRedirect(self.action.get_absolute_url())
        d = form.cleaned_data
        try:
            services.add_update(self.action, actor=request.user, body=d["body"], progress_percent=d["progress_percent"],
                                kind="correction" if d["corrects"] else "progress", corrects=d["corrects"])
        except ValidationError as exc:
            messages.error(request, _text(exc))
        else:
            messages.success(request, "Progress recorded.")
        return HttpResponseRedirect(self.action.get_absolute_url())


# -- evidence -----------------------------------------------------------------------------------------------------------------
class ActionEvidenceUploadView(ActionObjectMixin, View):
    """POST (multipart): ``purpose``, ``file`` and an optional ``caption``.  The target is *resolved* by the evidence registry, never trusted."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        from apps.evidence import services as evidence_services

        form = EvidenceForm(request.POST)
        upload = request.FILES.get("file")
        too_big = int(request.META.get("CONTENT_LENGTH") or 0) > int(settings.EVIDENCE_MAX_REQUEST_BYTES)
        if not form.is_valid() or upload is None or too_big:
            messages.error(request, "The file is too large." if too_big else "Choose a stage and a file to upload.")
            return HttpResponseRedirect(self.action.get_absolute_url())
        try:
            evidence_services.upload(request.user, target_type="corrective_action", ref={"corrective_action_id": str(self.action.pk), "purpose": form.cleaned_data["purpose"]},
                                     filename=upload.name, data=upload.read(int(settings.EVIDENCE_MAX_FILE_BYTES) + 1), caption=form.cleaned_data["caption"])
        except evidence_services.EvidenceError as exc:
            messages.error(request, " ".join(exc.messages))
        except PermissionDenied:
            messages.error(request, "You cannot add that kind of evidence to this action.")
        else:
            messages.success(request, "File uploaded. It counts once the security check passes.")
        return HttpResponseRedirect(self.action.get_absolute_url())


class _AttachmentMixin(ActionObjectMixin):
    def attachment(self, pk: Any) -> Any:
        from apps.evidence import selectors as evidence_selectors

        found = evidence_selectors.get_attachment(self.request.user, pk)
        if found is None or found.corrective_action_id != self.action.pk:  # an attachment of any other record is unreachable through this action
            raise Http404
        return found


class ActionEvidenceDetachView(_AttachmentMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: Any, attachment: Any) -> HttpResponse:
        from apps.evidence import services as evidence_services

        try:
            evidence_services.detach(request.user, self.attachment(attachment), reason=request.POST.get("reason", "Removed by the user"))
        except evidence_services.EvidenceError as exc:
            messages.error(request, " ".join(exc.messages))
        except PermissionDenied:
            messages.error(request, "You cannot remove that file.")
        else:
            messages.success(request, "File removed (it stays in the record).")
        return HttpResponseRedirect(self.action.get_absolute_url())


class ActionEvidenceFileView(_AttachmentMixin, View):
    http_method_names = ["get"]
    inline = False

    def get(self, request: HttpRequest, pk: Any, attachment: Any) -> HttpResponse:
        from apps.evidence import services as evidence_services
        from apps.evidence.selectors import is_servable
        from apps.evidence.validation import IMAGE_TYPES

        found = self.attachment(attachment)
        asset = found.asset
        if not is_servable(asset) or (self.inline and asset.content_type not in IMAGE_TYPES):
            raise Http404  # quarantined, unchecked or not an image: indistinguishable
        try:
            data = evidence_services.read_bytes(asset)
        except evidence_services.EvidenceError:
            raise Http404 from None
        evidence_services.record_access(request.user, asset, "preview" if self.inline else "download")
        response = HttpResponse(data, content_type=asset.content_type)
        response["Content-Disposition"] = f"{'inline' if self.inline else 'attachment'}; filename*=UTF-8''{quote(asset.original_name)}"
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        response["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'none'; script-src 'none'"
        response["Cross-Origin-Resource-Policy"] = "same-origin"
        response["Content-Length"] = str(len(data))
        return response


class ActionEvidencePreviewView(ActionEvidenceFileView):
    inline = True


class AssigneeOptionsView(ActionAccessMixin, TemplateView):
    """HTMX: users who may be made responsible at the chosen finding's location (only for findings the caller may add actions to)."""

    template_name = "corrective_actions/_assignee_options.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        raw = self.request.GET.get("finding", "")
        finding = selectors.creatable_findings(self.request.user).filter(pk=raw).first() if raw and len(raw) == 36 else None
        users = selectors.eligible_assignees(finding.organization, finding.location) if finding else []
        return super().get_context_data(users=users, **kwargs)


_ = reverse
