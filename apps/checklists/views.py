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
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.template.loader import render_to_string
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
from . import lifecycle, policies, selectors, services
from .comparison import CATEGORIES, IMPACTS, KINDS
from .filters import TemplateFilter
from .manifest import verify_version_integrity
from .publication import validate_for_publication
from .answer_types import ConfigurationError, registry
from .answer_types.preview import preview_context
from .forms import DuplicateForm, ItemForm, OptionForm, SectionForm, TemplateForm, VersionForm, build_config_form
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
def _flash_lifecycle_error(request: HttpRequest, exc: ValidationError) -> None:
    """Turn a lifecycle/validation refusal into messages (one per problem; never a stack trace)."""
    for problem in exc.messages:
        messages.error(request, problem)


class VersionListView(TemplateObjectMixin, TemplateView):
    """The version history: every version, newest first, paginated; read-only for anyone who can view the checklist."""

    template_name = "checklists/version_list.html"
    paginate_by = 20

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user, template = self.request.user, self.template
        page = Paginator(selectors.versions_of(template), self.paginate_by).get_page(self.request.GET.get("page"))
        draft = self.draft
        can_create = policies.can_create_version(user, template) and draft is None and template.status != c.TemplateStatus.ARCHIVED
        rows = list(page.object_list)
        compare = reverse("checklists:compare", args=[template.pk])
        for row in rows:  # "compare with the previous version" (numbers are contiguous: a draft is always the newest)
            row.compare_url = f"{compare}?from={row.version_number - 1}&to={row.version_number}" if row.version_number > 1 else ""
        context.update(
            template=template, versions=rows, page_obj=page, draft=draft,
            current=selectors.current_published_version(template),
            can_create_version=can_create, can_compare=policies.can_compare_versions(user, template) and page.paginator.count > 1,
            can_create_draft_from=can_create, form=VersionForm(),
        )
        return context


class VersionDetailView(TemplateObjectMixin, TemplateView):
    """A read-only view of any version's structure, its publication record and its integrity."""

    template_name = "checklists/version_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        version = selectors.get_version(self.template, self.kwargs["number"])
        if version is None:
            raise Http404
        user, template = self.request.user, self.template
        can_draft = (policies.can_create_version(user, template) and self.draft is None and not version.is_draft
                     and template.status != c.TemplateStatus.ARCHIVED)
        context.update(
            template=template, version=version, version_label=f"Version {version.version_number}",
            sections=list(selectors.get_sections_for_version(version)),
            can_edit=policies.can_edit_version(user, version), can_create_draft_from=can_draft,
            integrity=verify_version_integrity(version),
            publication_warnings=(version.publication_report or {}).get("warnings", []),
            previous_number=version.version_number - 1 if version.version_number > 1 else None,
            assignable=lifecycle.is_assignable(version),
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


class VersionDraftFromView(TemplateObjectMixin, View):
    """POST: a new draft that is a copy of *this* (published or superseded) version - the way to roll back."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        source = selectors.get_version(self.template, self.kwargs["number"])
        if source is None:
            raise Http404
        if not policies.can_create_version(request.user, self.template):
            raise PermissionDenied
        try:
            version = services.create_checklist_version(
                self.template, actor=request.user, change_summary=request.POST.get("change_summary", ""), source=source
            )
        except ValidationError as exc:
            _flash_lifecycle_error(request, exc)
            return HttpResponseRedirect(reverse("checklists:version_detail", args=[self.template.pk, source.version_number]))
        messages.success(request, f"Draft version {version.version_number} created as a copy of version {source.version_number}.")
        return HttpResponseRedirect(self.builder_url())


class VersionPublishView(TemplateObjectMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        version = selectors.get_version(self.template, self.kwargs["number"])
        if version is None:
            raise Http404
        if not policies.can_publish_template(request.user, self.template):  # status-independent: a repeated click is a message, not a 403
            raise PermissionDenied
        try:
            services.publish_checklist_version(
                version, actor=request.user, change_summary=request.POST.get("change_summary"),
                expected_hash=request.POST.get("expected_hash") or None,
            )
        except lifecycle.ConflictError as exc:
            if exc.lifecycle_code == "already_published":  # a repeated click or a retried request: not an error
                messages.info(request, _text(exc))
                return HttpResponseRedirect(reverse("checklists:version_detail", args=[self.template.pk, version.version_number]))
            _flash_lifecycle_error(request, exc)
            return HttpResponseRedirect(self.builder_url())
        except ValidationError as exc:
            if isinstance(exc, lifecycle.PublicationBlocked):
                messages.error(request, "The draft contains validation errors and cannot be published.")
            _flash_lifecycle_error(request, exc)
            return HttpResponseRedirect(self.builder_url())
        messages.success(request, f"Version {version.version_number} published. It can no longer be edited.")
        return HttpResponseRedirect(reverse("checklists:detail", args=[self.template.pk]))


class VersionValidateView(TemplateObjectMixin, View):
    """POST: run the publication validation of a draft.  HTMX gets the report fragment, a plain post gets messages."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        version = selectors.get_version(self.template, self.kwargs["number"])
        if version is None:
            raise Http404
        if not policies.can_validate_version(request.user, version):
            raise PermissionDenied
        report = lifecycle.run_validation(version, actor=request.user)
        if request.headers.get("HX-Request") == "true":
            return HttpResponse(render_to_string("checklists/_validation_report.html", {"report": report}, request=request))
        if report.ok:
            messages.success(request, "The draft is valid and can be published." + (f" {len(report.warnings)} warning(s)." if report.warnings else ""))
        else:
            for finding in report.errors:
                messages.error(request, finding.message)
        return HttpResponseRedirect(self.builder_url())


class VersionDiscardView(TemplateObjectMixin, View):
    """POST: delete a draft that was never published.  Published and superseded versions are kept for history."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        version = selectors.get_version(self.template, self.kwargs["number"])
        if version is None:
            raise Http404
        if not policies.can_update_template(request.user, self.template):  # the service explains why a published version is refused
            raise PermissionDenied
        try:
            lifecycle.discard_draft(version, actor=request.user)
        except ValidationError as exc:
            _flash_lifecycle_error(request, exc)
            return HttpResponseRedirect(self.builder_url())
        messages.success(request, f"Draft version {version.version_number} discarded. Published versions are unchanged.")
        return HttpResponseRedirect(reverse("checklists:versions", args=[self.template.pk]))


def _item_preview_html(item: Any) -> str:
    """The inert preview control of one item (the Phase 5 renderer; no input is live)."""
    definition = registry.get(item.answer_type)
    if definition.code in ("yes_no", "yes_no_na"):
        labels = [label for _value, label in c.FIXED_OPTIONS[definition.code]]
    else:
        labels = [o.label for o in item.options.all() if o.is_active]
    context = preview_context(definition, definition.config_with_defaults(item.config), options=labels)
    return render_to_string("checklists/_answer_preview.html", {**context, "hide_question": True})


class VersionPreviewView(TemplateObjectMixin, TemplateView):
    """A safe preview of any version: sections, questions, controls, options, rules - nothing is answered or saved."""

    template_name = "checklists/preview.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        from .rules import describe_rule, snapshot_for_version

        context = super().get_context_data(**kwargs)
        version = selectors.get_version(self.template, self.kwargs["number"])
        if version is None:
            raise Http404
        sections = list(selectors.get_sections_for_version(version))
        snapshot = snapshot_for_version(version)
        lines: dict[tuple[str, str], list[str]] = {}
        shown: set[tuple[str, str]] = set()
        for rule in snapshot.rules:
            if not rule.is_active or not isinstance(rule.actions, list):
                continue
            try:
                summary = f"{rule.code}: " + describe_rule(snapshot, rule.condition, rule.actions)
            except (KeyError, TypeError, AttributeError):
                continue
            for action in rule.actions:
                target = action.get("target") if isinstance(action, dict) else None
                if not target:
                    continue
                key = (target.get("type", ""), target.get("code", ""))
                lines.setdefault(key, []).append(summary)
                if action.get("type") in ("SHOW_ITEM", "SHOW_SECTION"):
                    shown.add(key)
        blocks = []
        for section in sections:
            entries = [{
                "item": item, "control": _item_preview_html(item), "rules": lines.get(("item", item.code), []),
                "hidden_by_default": ("item", item.code) in shown,
            } for item in section.items.all()]
            blocks.append({"section": section, "items": entries, "hidden_by_default": ("section", section.code) in shown})
        context.update(
            template=self.template, version=version, version_label=f"Version {version.version_number}", blocks=blocks,
            can_edit=policies.can_edit_version(self.request.user, version),
        )
        return context


class VersionCompareView(TemplateObjectMixin, TemplateView):
    """Compare two versions of this checklist (read-only; filters narrow the list)."""

    template_name = "checklists/compare.html"
    GROUPS = (("section", "Sections", "layout-list"), ("item", "Questions", "list-checks"), ("option", "Options", "list"), ("rule", "Rules", "git-branch"))

    def _number(self, key: str, default: int | None) -> int | None:
        raw = self.request.GET.get(key, "")
        if not raw:
            return default
        if not (raw.isascii() and raw.isdigit()):
            raise Http404
        return int(raw)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        template = self.template
        versions = list(selectors.versions_of(template))
        if not versions:
            raise Http404
        by_number = {v.version_number: v for v in versions}
        newest = next((v for v in versions if not v.is_draft), versions[0])
        to_number = self._number("to", newest.version_number)
        default_from = newest.based_on.version_number if newest.based_on_id else max(newest.version_number - 1, 1)
        from_number = self._number("from", default_from)
        old, new = by_number.get(from_number), by_number.get(to_number)
        if old is None or new is None:
            raise Http404
        comparison = lifecycle.compare_versions(template, old, new, actor=self.request.user)
        filters = {"category": self.request.GET.get("category", ""), "kind": self.request.GET.get("kind", ""), "impact": self.request.GET.get("impact", "")}
        for key, allowed in (("category", CATEGORIES), ("kind", KINDS), ("impact", IMPACTS)):
            if filters[key] not in allowed:
                filters[key] = ""
        shown = comparison.filtered(**filters)
        counts = comparison.counts()
        counts["plural"] = "" if counts["total"] == 1 else "s"
        groups = [{"key": key, "label": label, "icon": icon, "changes": [ch for ch in shown if ch.category == key]} for key, label, icon in self.GROUPS]
        context.update(
            template=template, versions=versions, old=old, new=new, comparison=comparison, counts=counts, filters=filters,
            groups=[g for g in groups if g["changes"]], summary_line=f"v{old.version_number} → v{new.version_number}",
            category_choices=[("section", "Sections"), ("item", "Questions"), ("option", "Options"), ("rule", "Rules")],
            kind_choices=[("added", "Added"), ("removed", "Removed"), ("modified", "Modified"), ("reordered", "Reordered")],
            impact_choices=[("behavior", "Changes behaviour"), ("wording", "Wording only"), ("structure", "Structure / order")],
        )
        return context


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
        report = validate_for_publication(draft) if draft else None
        context.update(
            template=template, draft=draft,
            can_edit=bool(draft) and policies.can_edit_version(user, draft),
            can_publish=bool(draft) and policies.can_publish_version(user, draft),
            can_create_version=policies.can_create_version(user, template) and draft is None
            and template.status != c.TemplateStatus.ARCHIVED,
            sections=list(selectors.get_sections_for_version(draft)) if draft else [],
            problems=report.messages() if report else [], report=report, draft_hash=report.draft_hash if report else "",
            can_validate=bool(draft) and policies.can_validate_version(user, draft),
            can_discard=bool(draft) and policies.can_discard_version(user, draft)
            and template.versions.exclude(pk=draft.pk).exists(),
            current=selectors.current_published_version(template),
            rule_count=draft.rules.count() if draft else 0,
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
    """The item editor: the general form plus a type-specific configuration form (and a live preview)."""

    form_class = ItemForm
    template_name = "checklists/item_form.html"
    item = None

    def authorize(self) -> None:
        self._draft_version = self.require_draft()
        self.setup_objects()

    def setup_objects(self) -> None:
        raise NotImplementedError

    def get_form_kwargs(self):  # noqa: ANN201
        kwargs = super().get_form_kwargs()
        kwargs["item"] = self.item
        return kwargs

    def selected_answer_type(self) -> str:
        if self.request.method == "POST":
            return self.request.POST.get("answer_type", "")
        return self.item.answer_type if self.item else c.AnswerType.YES_NO

    @property
    def config_form(self):  # noqa: ANN201
        if not hasattr(self, "_config_form"):
            answer_type = self.selected_answer_type()
            stored = self.item.config if self.item and answer_type == self.item.answer_type else None
            data = self.request.POST if self.request.method == "POST" else None
            self._config_form = build_config_form(answer_type, data=data, config=stored)
        return self._config_form

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = self.get_form()
        # Both forms are validated so every problem is shown at once.
        valid = form.is_valid() & self.config_form.is_valid()
        return self.form_valid(form) if valid else self.form_invalid(form)

    def get_context_data(self, **kwargs):  # noqa: ANN003, ANN201
        item = self.item
        answer_type = self.selected_answer_type()
        definition = registry.get(answer_type) if answer_type in registry else registry.get(c.AnswerType.YES_NO)
        return super().get_context_data(
            template=self.template, section=self.section, item=item, option_form=OptionForm(),
            options=list(item.options.order_by("sort_order")) if item else [],
            custom_options=bool(item) and item.answer_type in c.CUSTOM_OPTIONS,
            fixed_options=bool(item) and item.answer_type in c.FIXED_OPTIONS,
            config_form=self.config_form, definition=definition, **kwargs,
        )


class ItemCreateView(ItemFormView):
    def setup_objects(self) -> None:
        self.section = _section(self, self._draft_version)

    def form_valid(self, form):  # noqa: ANN001, ANN201
        data = form.cleaned_data
        try:
            item = services.create_item(
                self.section, question=data["question"], code=data["code"], answer_type=data["answer_type"],
                config=self.config_form.to_config(), description=data["description"], help_text=data["help_text"],
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
                answer_type=data["answer_type"], config=self.config_form.to_config(), description=data["description"],
                help_text=data["help_text"], instructions=data["instructions"], is_required=data["is_required"],
                is_active=data["is_active"],
            )
        except ValidationError as exc:
            add_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, "Item saved.")
        return HttpResponseRedirect(reverse("checklists:item_edit", args=[self.template.pk, self.item.pk]))


class _AnswerTypePartialView(TemplateObjectMixin, TemplateView):
    """Base of the two HTMX partials of the item editor (both GET, both need edit rights on the draft)."""

    def authorize(self) -> None:
        self._draft_version = self.require_draft()

    def requested_type(self) -> str:
        code = self.request.GET.get("answer_type", "")
        return code if code in registry else c.AnswerType.YES_NO

    def requested_item(self):  # noqa: ANN201
        """The item being edited (only from this draft - a foreign id is simply ignored)."""
        raw = self.request.GET.get("item")
        return selectors.get_item(self._draft_version, raw) if raw else None


class AnswerTypeFieldsView(_AnswerTypePartialView):
    """HTMX: the configuration fields for the selected answer type."""

    template_name = "checklists/_answer_config.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        answer_type, item = self.requested_type(), self.requested_item()
        stored = item.config if item and item.answer_type == answer_type else None
        return super().get_context_data(
            definition=registry.get(answer_type), config_form=build_config_form(answer_type, config=stored),
            item=item, **kwargs,
        )


class AnswerTypePreviewView(_AnswerTypePartialView):
    """HTMX: a disabled preview of the item as currently configured (design-time only)."""

    template_name = "checklists/_answer_preview.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        answer_type = self.requested_type()
        definition, item = registry.get(answer_type), self.requested_item()
        form = build_config_form(answer_type, data=self.request.GET)
        context: dict[str, Any] = {"question": self.request.GET.get("question", "").strip()[:500], "definition": definition}
        if not form.is_valid():
            return super().get_context_data(error="Fix the highlighted settings to see the preview.", **context, **kwargs)
        try:
            config = definition.clean_config(form.to_config())
        except ConfigurationError as exc:
            return super().get_context_data(error=exc.message, **context, **kwargs)
        labels = [o.label for o in item.options.filter(is_active=True).order_by("sort_order")] if item else []
        if definition.code in ("yes_no", "yes_no_na"):
            labels = [label for _v, label in c.FIXED_OPTIONS[definition.code]]
        context.update(preview_context(definition, config, question=context["question"], options=labels))
        return super().get_context_data(**context, **kwargs)


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
