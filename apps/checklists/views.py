"""Class-based views for checklist definitions: list, detail, builder.

Pattern (same as every other domain): the template is fetched through ``selectors.get_template`` (not
visible -> 404), the action is authorized by a policy (visible but forbidden -> 403), and every write is a
service call whose ``ValidationError`` becomes a message.  The builder edits the template's single *draft*
version; published versions are read-only (the services refuse changes and the UI offers none).
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
from apps.core.models import ActivityLog
from apps.permissions.authorization import has_permission_anywhere
from apps.permissions.registry import P

from . import constants as c
from . import policies, selectors, services
from .filters import TemplateFilter
from .forms import DuplicateForm, ItemForm, OptionForm, SectionForm, TemplateForm, VersionForm
from .models import ChecklistOption
from .tables import TemplateTable


def _text(exc: ValidationError) -> str:
    return " ".join(exc.messages)


class ChecklistAccessMixin(LoginRequiredMixin):
    """Collection-level gate: the user must be able to view checklists in at least one organization."""

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated:
            if not has_permission_anywhere(request.user, P.CHECKLIST_VIEW):
                raise PermissionDenied
            self.authorize()
        return super().dispatch(request, *args, **kwargs)

    def authorize(self) -> None:
        """Per-view authorization hook, run for authenticated users before the handler."""


class TemplateObjectMixin(ChecklistAccessMixin):
    """Resolves ``self.template`` from the URL through the authorized selector (else 404)."""

    @property
    def template(self):  # noqa: ANN201
        if not hasattr(self, "_template"):
            template = selectors.get_template(self.request.user, self.kwargs["pk"])
            if template is None:
                raise Http404
            self._template = template
        return self._template

    @property
    def draft(self):  # noqa: ANN201
        if not hasattr(self, "_draft"):
            self._draft = selectors.get_draft_version(self.template)
        return self._draft

    def require_draft(self):  # noqa: ANN201
        """The editable draft version, or 404 (nothing to edit) / 403 (not allowed to edit it)."""
        draft = self.draft
        if draft is None:
            raise Http404
        if not policies.can_edit_version(self.request.user, draft):
            raise PermissionDenied
        return draft

    def builder_url(self) -> str:
        return reverse("checklists:builder", args=[self.template.pk])


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
class TemplateListView(ChecklistAccessMixin, SingleTableMixin, FilterView):
    table_class = TemplateTable
    filterset_class = TemplateFilter
    template_name = "checklists/template_list.html"
    context_object_name = "templates"
    table_pagination = {"per_page": 20}

    def get_queryset(self):  # noqa: ANN201
        return selectors.templates_for_user(self.request.user)  # authorize first, then search/filter/paginate

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        context = super().get_context_data(**kwargs)
        context["can_create"] = policies.can_create_template(self.request.user)
        context["hidden_filters"] = ()
        return context


class TemplateCreateView(LoginRequiredMixin, FormView):
    form_class = TemplateForm
    template_name = "checklists/template_form.html"

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated and not policies.can_create_template(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, organization=get_active_organization(self.request))
        return kwargs

    def form_valid(self, form):  # noqa: ANN001, ANN201
        data = form.cleaned_data
        organization = data["organization"]
        if not policies.can_create_template(self.request.user, organization):
            raise PermissionDenied
        try:
            category = data["category"]
            if data["new_category"].strip():
                if not policies.can_manage_categories(self.request.user, organization):
                    raise PermissionDenied
                category = services.create_category(organization=organization, name=data["new_category"], actor=self.request.user)
            template = services.create_checklist_template(
                organization=organization, name=data["name"], code=data["code"], actor=self.request.user,
                description=data["description"], instructions=data["instructions"],
                estimated_duration_minutes=data["estimated_duration_minutes"], category=category,
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, f"Checklist “{template.name}” created. Add sections and items in the builder.")
        return HttpResponseRedirect(reverse("checklists:builder", args=[template.pk]))


class TemplateDetailView(TemplateObjectMixin, TemplateView):
    template_name = "checklists/template_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        template, user = self.template, self.request.user
        versions = list(selectors.versions_of(template))
        shown = next((v for v in versions if v.status == c.VersionStatus.PUBLISHED), versions[0] if versions else None)
        context.update(
            template=template,
            versions=versions,
            shown_version=shown,
            sections=list(selectors.get_sections_for_version(shown)) if shown else [],
            draft=next((v for v in versions if v.is_draft), None),
            activity=selectors.template_activity(template),
            can_update=policies.can_update_template(user, template),
            can_duplicate=policies.can_duplicate_template(user, template),
            can_manage=policies.can_manage_template(user, template),
            can_delete=policies.can_delete_template(user, template),
            can_publish=any(policies.can_publish_version(user, v) for v in versions),
            published_exists=any(v.status == c.VersionStatus.PUBLISHED for v in versions),
            detail_tabs=[
                {"key": "structure", "label": "Structure", "icon": "layout-list"},
                {"key": "versions", "label": "Versions", "icon": "history"},
                {"key": "activity", "label": "Activity", "icon": "activity"},
            ],
        )
        return context


class TemplateUpdateView(TemplateObjectMixin, FormView):
    form_class = TemplateForm
    template_name = "checklists/template_form.html"

    def authorize(self) -> None:
        if not policies.can_update_template(self.request.user, self.template):
            raise PermissionDenied

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, template=self.template)
        return kwargs

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        return super().get_context_data(template=self.template, **kwargs)

    def form_valid(self, form):  # noqa: ANN001, ANN201
        data = form.cleaned_data
        try:
            category = data["category"]
            if data["new_category"].strip():
                if not policies.can_manage_categories(self.request.user, self.template.organization_id):
                    raise PermissionDenied
                category = services.create_category(organization=self.template.organization, name=data["new_category"], actor=self.request.user)
            services.update_checklist_template(
                self.template, actor=self.request.user, name=data["name"], description=data["description"],
                instructions=data["instructions"], estimated_duration_minutes=data["estimated_duration_minutes"],
                category=category,
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, "Checklist details saved.")
        return HttpResponseRedirect(reverse("checklists:detail", args=[self.template.pk]))


class TemplateDuplicateView(TemplateObjectMixin, FormView):
    form_class = DuplicateForm
    template_name = "checklists/duplicate_form.html"

    def authorize(self) -> None:
        if not policies.can_duplicate_template(self.request.user, self.template):
            raise PermissionDenied

    def get_initial(self):  # noqa: ANN201
        return {"name": f"{self.template.name} - Copy", "code": f"{self.template.code}-COPY"}

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        return super().get_context_data(template=self.template, **kwargs)

    def form_valid(self, form):  # noqa: ANN001, ANN201
        try:
            copy = services.duplicate_checklist_template(
                self.template, actor=self.request.user, name=form.cleaned_data["name"], code=form.cleaned_data["code"]
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, f"Duplicated as “{copy.name}”.")
        return HttpResponseRedirect(reverse("checklists:detail", args=[copy.pk]))


class TemplateStatusView(TemplateObjectMixin, View):
    """POST: archive | restore | activate | delete."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        template, user, action = self.template, request.user, request.POST.get("action")
        table = {
            "archive": (policies.can_manage_template, services.archive_checklist_template, "archived"),
            "restore": (policies.can_manage_template, services.restore_checklist_template, "restored"),
            "activate": (policies.can_update_template, services.activate_checklist_template, "activated"),
        }
        if action == "delete":
            if not policies.can_delete_template(user, template):
                raise PermissionDenied
            try:
                services.delete_checklist_template(template, actor=user)
            except ValidationError as exc:
                messages.error(request, _text(exc))
                return HttpResponseRedirect(reverse("checklists:detail", args=[template.pk]))
            messages.success(request, "Draft checklist deleted.")
            return HttpResponseRedirect(reverse("checklists:list"))
        if action not in table:
            messages.error(request, "Unknown action.")
            return HttpResponseRedirect(reverse("checklists:detail", args=[template.pk]))
        allowed, service, past = table[action]
        if not allowed(user, template):
            raise PermissionDenied
        try:
            service(template, actor=user)
        except ValidationError as exc:
            messages.error(request, _text(exc))
        else:
            messages.success(request, f"Checklist “{template.name}” {past}.")
        return HttpResponseRedirect(reverse("checklists:detail", args=[template.pk]))


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------
class VersionListView(TemplateObjectMixin, TemplateView):
    template_name = "checklists/version_list.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update(
            template=self.template, versions=list(selectors.versions_of(self.template)),
            can_create_version=policies.can_create_version(self.request.user, self.template)
            and self.draft is None and self.template.status != c.TemplateStatus.ARCHIVED,
            form=VersionForm(),
        )
        return context


class VersionDetailView(TemplateObjectMixin, TemplateView):
    """A read-only view of any version's structure."""

    template_name = "checklists/version_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        version = selectors.get_version(self.template, self.kwargs["number"])
        if version is None:
            raise Http404
        context.update(
            template=self.template, version=version, version_label=f"Version {version.version_number}",
            sections=list(selectors.get_sections_for_version(version)),
            can_edit=policies.can_edit_version(self.request.user, version),
        )
        return context


class VersionCreateView(TemplateObjectMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not policies.can_create_version(request.user, self.template):
            raise PermissionDenied
        form = VersionForm(request.POST)
        try:
            version = services.create_checklist_version(
                self.template, actor=request.user, change_summary=form.data.get("change_summary", "")
            )
        except ValidationError as exc:
            messages.error(request, _text(exc))
            return HttpResponseRedirect(reverse("checklists:versions", args=[self.template.pk]))
        messages.success(request, f"Draft version {version.version_number} created from the latest version.")
        return HttpResponseRedirect(self.builder_url())


class VersionPublishView(TemplateObjectMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        version = selectors.get_version(self.template, self.kwargs["number"])
        if version is None:
            raise Http404
        if not policies.can_publish_version(request.user, version):
            raise PermissionDenied
        try:
            services.publish_checklist_version(version, actor=request.user, change_summary=request.POST.get("change_summary"))
        except ValidationError as exc:
            for problem in exc.messages:
                messages.error(request, problem)
            return HttpResponseRedirect(self.builder_url())
        messages.success(request, f"Version {version.version_number} published. It can no longer be edited.")
        return HttpResponseRedirect(reverse("checklists:detail", args=[self.template.pk]))


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
class BuilderView(TemplateObjectMixin, TemplateView):
    template_name = "checklists/builder.html"

    def authorize(self) -> None:
        if not policies.can_update_template(self.request.user, self.template):
            raise PermissionDenied

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        template, draft, user = self.template, self.draft, self.request.user
        context.update(
            template=template, draft=draft,
            can_edit=bool(draft) and policies.can_edit_version(user, draft),
            can_publish=bool(draft) and policies.can_publish_version(user, draft),
            can_create_version=policies.can_create_version(user, template) and draft is None
            and template.status != c.TemplateStatus.ARCHIVED,
            sections=list(selectors.get_sections_for_version(draft)) if draft else [],
            problems=services.validate_version_complete(draft) if draft else [],
            version_form=VersionForm(),
            latest_published=template.versions.filter(status=c.VersionStatus.PUBLISHED).order_by("-version_number").first(),
        )
        return context


class BuilderPostView(TemplateObjectMixin, View):
    """Base for the builder's small POST actions: authorize, call a service, flash the result."""

    http_method_names = ["post"]
    success_message = "Saved."

    def perform(self, draft: Any) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def redirect_url(self) -> str:
        return self.builder_url()

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        draft = self.require_draft()
        try:
            self.perform(draft)
        except ValidationError as exc:
            messages.error(request, _text(exc))
        else:
            messages.success(request, self.success_message)
        return HttpResponseRedirect(self.redirect_url())

    def direction(self) -> str:
        return self.request.POST.get("direction", "")


def _section(view: TemplateObjectMixin, draft: Any):  # noqa: ANN201
    section = selectors.get_section(draft, view.kwargs["section_pk"])
    if section is None:
        raise Http404
    return section


def _item(view: TemplateObjectMixin, draft: Any):  # noqa: ANN201
    item = selectors.get_item(draft, view.kwargs["item_pk"])
    if item is None:
        raise Http404
    return item


class SectionFormView(TemplateObjectMixin, FormView):
    form_class = SectionForm
    template_name = "checklists/section_form.html"

    def get_section(self, draft: Any):  # noqa: ANN401, ANN201
        return None

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["section"] = self._section_obj
        return kwargs

    def authorize(self) -> None:
        self._draft_version = self.require_draft()
        self._section_obj = self.get_section(self._draft_version)

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        return super().get_context_data(template=self.template, section=self._section_obj, **kwargs)

    def save(self, form: SectionForm) -> Any:  # noqa: ANN401
        raise NotImplementedError

    def form_valid(self, form):  # noqa: ANN001, ANN201
        try:
            self.save(form)
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, "Section saved.")
        return HttpResponseRedirect(self.builder_url())


class SectionCreateView(SectionFormView):
    def save(self, form):  # noqa: ANN001, ANN201
        data = form.cleaned_data
        return services.create_section(self._draft_version, name=data["name"], actor=self.request.user,
                                       description=data["description"], instructions=data["instructions"],
                                       is_required=data["is_required"])


class SectionUpdateView(SectionFormView):
    def get_section(self, draft):  # noqa: ANN001, ANN201
        return _section(self, draft)

    def save(self, form):  # noqa: ANN001, ANN201
        return services.update_section(self._section_obj, actor=self.request.user, **form.cleaned_data)


class SectionDeleteView(BuilderPostView):
    success_message = "Section removed."

    def perform(self, draft):  # noqa: ANN001, ANN201
        services.delete_section(_section(self, draft), actor=self.request.user)


class SectionMoveView(BuilderPostView):
    success_message = "Section moved."

    def perform(self, draft):  # noqa: ANN001, ANN201
        services.move_section(_section(self, draft), self.direction(), actor=self.request.user)


class ItemFormView(TemplateObjectMixin, FormView):
    form_class = ItemForm
    template_name = "checklists/item_form.html"

    def authorize(self) -> None:
        self._draft_version = self.require_draft()
        self.setup_objects()

    def setup_objects(self) -> None:
        raise NotImplementedError

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["item"] = getattr(self, "item", None)
        return kwargs

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        item = getattr(self, "item", None)
        return super().get_context_data(
            template=self.template, section=self.section, item=item, option_form=OptionForm(),
            options=list(item.options.order_by("sort_order")) if item else [],
            custom_options=bool(item) and item.answer_type in c.CUSTOM_OPTIONS,
            fixed_options=bool(item) and item.answer_type in c.FIXED_OPTIONS,
            answer_types=c.AnswerType.choices, **kwargs,
        )


class ItemCreateView(ItemFormView):
    def setup_objects(self) -> None:
        self.section = _section(self, self._draft_version)

    def form_valid(self, form):  # noqa: ANN001, ANN201
        data = form.cleaned_data
        try:
            item = services.create_item(
                self.section, question=data["question"], code=data["code"], answer_type=data["answer_type"],
                config=data["config"], description=data["description"], help_text=data["help_text"],
                instructions=data["instructions"], is_required=data["is_required"], is_active=data["is_active"],
                actor=self.request.user,
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        if item.answer_type in c.CUSTOM_OPTIONS:
            messages.success(self.request, "Item added. Now add its options.")
            return HttpResponseRedirect(reverse("checklists:item_edit", args=[self.template.pk, item.pk]))
        messages.success(self.request, "Item added.")
        return HttpResponseRedirect(self.builder_url())


class ItemUpdateView(ItemFormView):
    def setup_objects(self) -> None:
        self.item = _item(self, self._draft_version)
        self.section = self.item.section

    def form_valid(self, form):  # noqa: ANN001, ANN201
        data = form.cleaned_data
        try:
            services.update_item(
                self.item, actor=self.request.user, question=data["question"], code=data["code"] or self.item.code,
                answer_type=data["answer_type"], config=data["config"], description=data["description"],
                help_text=data["help_text"], instructions=data["instructions"], is_required=data["is_required"],
                is_active=data["is_active"],
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, "Item saved.")
        return HttpResponseRedirect(reverse("checklists:item_edit", args=[self.template.pk, self.item.pk]))


class ItemDeleteView(BuilderPostView):
    success_message = "Item removed."

    def perform(self, draft):  # noqa: ANN001, ANN201
        services.delete_item(_item(self, draft), actor=self.request.user)


class ItemMoveView(BuilderPostView):
    success_message = "Item moved."

    def perform(self, draft):  # noqa: ANN001, ANN201
        services.move_item(_item(self, draft), self.direction(), actor=self.request.user)


class OptionAddView(BuilderPostView):
    success_message = "Option added."

    def redirect_url(self) -> str:
        return reverse("checklists:item_edit", args=[self.template.pk, self.kwargs["item_pk"]])

    def perform(self, draft):  # noqa: ANN001, ANN201
        form = OptionForm(self.request.POST)
        if not form.is_valid():
            raise ValidationError(form.errors["label"])
        services.create_option(_item(self, draft), label=form.cleaned_data["label"], actor=self.request.user)


class _OptionPostView(BuilderPostView):
    def redirect_url(self) -> str:
        return reverse("checklists:item_edit", args=[self.template.pk, self.kwargs["item_pk"]])

    def option(self, draft: Any) -> ChecklistOption:
        item = _item(self, draft)
        option = item.options.filter(pk=self.kwargs["option_pk"]).first()
        if option is None:
            raise Http404
        return option


class OptionUpdateView(_OptionPostView):
    success_message = "Option saved."

    def perform(self, draft):  # noqa: ANN001, ANN201
        option = self.option(draft)
        form = OptionForm(self.request.POST)
        if not form.is_valid():
            raise ValidationError(form.errors["label"])
        services.update_option(option, actor=self.request.user, label=form.cleaned_data["label"],
                               is_active=self.request.POST.get("is_active", "on") != "off")


class OptionDeleteView(_OptionPostView):
    success_message = "Option removed."

    def perform(self, draft):  # noqa: ANN001, ANN201
        services.delete_option(self.option(draft), actor=self.request.user)


class OptionMoveView(_OptionPostView):
    success_message = "Option moved."

    def perform(self, draft):  # noqa: ANN001, ANN201
        services.move_option(self.option(draft), self.direction(), actor=self.request.user)
