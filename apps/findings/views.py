"""Class-based views for findings: list, detail, create/edit, lifecycle actions, evidence files and the organization's configuration.

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
from django.urls import reverse
from django.views import View
from django.views.generic import FormView, TemplateView
from django_filters.views import FilterView
from django_tables2 import SingleTableMixin

from apps.accounts.context import get_active_organization
from apps.core.forms import add_validation_error
from apps.permissions.authorization import has_permission_anywhere
from apps.permissions.registry import P

from . import policies, selectors, services
from .files import evidence_response
from .filters import FindingFilter
from .forms import ActionForm, AssignForm, CategoryForm, EditForm, FindingForm, SeverityForm
from .models import FindingCategory, FindingSeverity
from .services import FindingConflict
from .tables import FindingTable


def _text(exc: ValidationError) -> str:
    return " ".join(exc.messages)


class FindingAccessMixin(LoginRequiredMixin):
    """Collection gate: the user may see findings somewhere (scope), or has been made responsible for one."""

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated:
            user = request.user
            if not (has_permission_anywhere(user, P.FINDING_VIEW) or selectors.findings_visible_to(user).exists()):
                raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class FindingObjectMixin(FindingAccessMixin):
    @property
    def finding(self):  # noqa: ANN201
        if not hasattr(self, "_finding"):
            found = selectors.get_finding(self.request.user, self.kwargs["pk"])
            if found is None:
                raise Http404
            self._finding = found
        return self._finding


# ---------------------------------------------------------------------------
class FindingListView(FindingAccessMixin, SingleTableMixin, FilterView):
    table_class = FindingTable
    filterset_class = FindingFilter
    template_name = "findings/finding_list.html"
    table_pagination = {"per_page": 20}

    def get_queryset(self):  # noqa: ANN201
        return selectors.findings_visible_to(self.request.user)  # authorize first, then search/filter/sort/paginate

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context.update(summary=selectors.summary(selectors.findings_visible_to(user)), can_create=has_permission_anywhere(user, P.FINDING_CREATE),
                       can_manage=has_permission_anywhere(user, P.FINDING_MANAGE), hidden_filters=())
        return context


class FindingDetailView(FindingObjectMixin, TemplateView):
    template_name = "findings/finding_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        f, user = self.finding, self.request.user
        from apps.evidence.selectors import is_servable
        from apps.executions import selectors as exec_selectors

        can_assign = policies.can_assign(user, f)
        evidence = selectors.evidence_for(f)
        execution = exec_selectors.get_execution(user, f.execution_id) if f.execution_id else None  # related record only if authorized
        context.update(
            finding=f, path=f.location.display_path, timeline=selectors.timeline(f), actions=policies.available_actions(user, f),
            can_update=policies.can_update(user, f), can_assign=can_assign,
            assignees=selectors.eligible_assignees(f.organization, f.location) if can_assign else [],
            evidence=[{"attachment": a, "servable": is_servable(a.asset), "image": a.asset.is_image} for a in evidence],
            related_execution=execution, item_question=f.item.question if f.item_id else "",
        )
        return context


# ---------------------------------------------------------------------------
class FindingCreateView(FindingAccessMixin, FormView):
    template_name = "findings/finding_form.html"
    form_class = FindingForm

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated and not has_permission_anywhere(request.user, P.FINDING_CREATE):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_initial(self):  # noqa: ANN201
        return {"location": self.request.GET["location"]} if self.request.GET.get("location") else {}

    def form_valid(self, form: FindingForm) -> HttpResponse:
        d = form.cleaned_data
        try:
            finding = services.create_manual(actor=self.request.user, location=d["location"], title=d["title"], description=d["description"],
                                             category=d["category"], severity=d["severity"], assigned_to=d["assigned_to"], due_at=d["due_at"])
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, f"{finding.reference} was recorded.")
        return HttpResponseRedirect(finding.get_absolute_url())


class FindingUpdateView(FindingObjectMixin, FormView):
    template_name = "findings/finding_form.html"
    form_class = EditForm

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["finding"] = self.finding
        return kwargs

    def get_initial(self):  # noqa: ANN201
        f = self.finding
        if not policies.can_update(self.request.user, f):
            raise PermissionDenied
        return {"title": f.title, "description": f.description, "category": f.category, "severity": f.severity, "due_at": f.due_at}

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not policies.can_update(request.user, self.finding):
            raise PermissionDenied
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(finding=self.finding, **kwargs)

    def form_valid(self, form: EditForm) -> HttpResponse:
        d = form.cleaned_data
        try:
            services.update_finding(self.finding, actor=self.request.user, title=d["title"], description=d["description"], category=d["category"],
                                    severity=d["severity"], due_at=d["due_at"])
        except FindingConflict as exc:
            messages.info(self.request, _text(exc))
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        else:
            messages.success(self.request, "Changes saved.")
        return HttpResponseRedirect(self.finding.get_absolute_url())


# ---------------------------------------------------------------------------
class FindingActionView(FindingObjectMixin, View):
    """POST-only lifecycle action: authorize in the service, flash the outcome, return to the detail page."""

    http_method_names = ["post"]
    action = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = ActionForm(request.POST)
        reason = form.cleaned_data["reason"] if form.is_valid() else ""
        try:
            finding = services.transition(self.finding, self.action, actor=request.user, reason=reason)
        except FindingConflict as exc:  # a repeated or racing request: informational, nothing changed
            messages.info(request, _text(exc))
        except ValidationError as exc:
            messages.error(request, _text(exc))
        else:
            messages.success(request, f"{finding.reference} is now {finding.get_status_display().lower()}.")
        return HttpResponseRedirect(self.finding.get_absolute_url())


class FindingAssignView(FindingObjectMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = AssignForm(request.POST)
        try:
            if not form.is_valid():
                raise ValidationError("Choose a valid user and date.")
            user = None
            if form.cleaned_data["assigned_to"]:
                user = selectors.eligible_assignees(self.finding.organization, self.finding.location).filter(pk=form.cleaned_data["assigned_to"]).first()
                if user is None:
                    raise ValidationError("That user cannot be made responsible for this finding.")
            due = {"due_at": form.cleaned_data["due_at"]} if "due_at" in request.POST else {}
            services.assign(self.finding, actor=request.user, assigned_to=user, **due)
        except ValidationError as exc:
            messages.error(request, _text(exc))
        else:
            messages.success(request, "Responsibility updated.")
        return HttpResponseRedirect(self.finding.get_absolute_url())


class FindingEvidenceFileView(FindingObjectMixin, View):
    """Serve one piece of a finding's evidence after re-checking the finding, the attachment's belonging and the file's state."""

    http_method_names = ["get"]
    inline = False

    def get(self, request: HttpRequest, pk: Any, attachment: Any) -> HttpResponse:
        response = evidence_response(request.user, self.finding, attachment, inline=self.inline)
        if response is None:
            raise Http404  # not part of this finding, quarantined, still unchecked or missing: indistinguishable
        return response

class FindingEvidencePreviewView(FindingEvidenceFileView):
    inline = True


class AssigneeOptionsView(FindingAccessMixin, TemplateView):
    """HTMX: the users who may be made responsible at the chosen location (only for locations the caller may raise findings at)."""

    template_name = "findings/_assignee_options.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        raw = self.request.GET.get("location", "")
        location = selectors.creatable_locations(self.request.user).filter(pk=raw).first() if raw and len(raw) == 36 else None
        users = selectors.eligible_assignees(location.organization, location) if location else []
        return super().get_context_data(users=users, **kwargs)


# ---------------------------------------------------------------------------
# Configuration (finding.manage, organization-wide): categories and severity levels of the *current* organization.
class ConfigAccessMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated:
            organization = get_active_organization(request)
            if organization is None or not policies.can_manage_config(request.user, organization):
                raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class FindingSettingsView(ConfigAccessMixin, TemplateView):
    template_name = "findings/settings.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        organization = get_active_organization(self.request)
        services.ensure_defaults(organization)
        return super().get_context_data(categories=selectors.categories_for(organization, active_only=False),
                                        severities=selectors.severities_for(organization, active_only=False).order_by("-rank"), **kwargs)


class _ConfigFormView(ConfigAccessMixin, FormView):
    template_name = "findings/config_form.html"
    model: Any = None
    heading = ""

    @property
    def instance(self):  # noqa: ANN201
        if not hasattr(self, "_instance"):
            pk = self.kwargs.get("pk")
            self._instance = None
            if pk:
                found = self.model.objects.filter(pk=pk, organization=get_active_organization(self.request)).first()
                if found is None:
                    raise Http404
                self._instance = found
        return self._instance

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(heading=self.heading, instance=self.instance, **kwargs)

    def get_success_url(self) -> str:
        return reverse("findings:settings")

    def save(self, data: dict[str, Any]) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def form_valid(self, form):  # noqa: ANN001, ANN201
        try:
            self.save(form.cleaned_data)
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, "Saved.")
        return HttpResponseRedirect(self.get_success_url())


class CategoryFormView(_ConfigFormView):
    form_class = CategoryForm
    model = FindingCategory
    heading = "Finding category"

    def get_initial(self):  # noqa: ANN201
        c = self.instance
        return {"code": c.code, "name": c.name, "description": c.description, "sort_order": c.sort_order, "is_active": c.is_active} if c else {}

    def get_form(self, form_class=None):  # noqa: ANN001, ANN201
        form = super().get_form(form_class)
        if self.instance:
            form.fields["code"].disabled = True
        return form

    def save(self, d: dict[str, Any]) -> Any:
        return services.save_category(actor=self.request.user, organization=get_active_organization(self.request), category=self.instance, code=d["code"], name=d["name"],
                                      description=d["description"], is_active=d["is_active"], sort_order=d["sort_order"])


class SeverityFormView(_ConfigFormView):
    form_class = SeverityForm
    model = FindingSeverity
    heading = "Severity level"

    def get_initial(self):  # noqa: ANN201
        s = self.instance
        return {"code": s.code, "name": s.name, "rank": s.rank, "requires_owner": s.requires_owner, "default_due_days": s.default_due_days,
                "is_escalation": s.is_escalation, "is_default": s.is_default, "is_active": s.is_active} if s else {}

    def get_form(self, form_class=None):  # noqa: ANN001, ANN201
        form = super().get_form(form_class)
        if self.instance:
            form.fields["code"].disabled = True
        return form

    def save(self, d: dict[str, Any]) -> Any:
        return services.save_severity(actor=self.request.user, organization=get_active_organization(self.request), severity=self.instance, code=d["code"], name=d["name"],
                                      rank=d["rank"], requires_owner=d["requires_owner"], default_due_days=d["default_due_days"],
                                      is_escalation=d["is_escalation"], is_default=d["is_default"], is_active=d["is_active"])

