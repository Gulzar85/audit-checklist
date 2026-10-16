"""Checklist admin.  The builder UI is the primary authoring tool; the admin is for support and inspection.

Published structure is read-only here too: sections, items, options and rules of a published or superseded version, and
those versions themselves, cannot be edited or deleted from the admin - inlines included - and if a form were ever
submitted anyway the model guards and the PostgreSQL triggers refuse the write (ADR 0049).  A draft's change summary is the
only version field the admin can edit; publishing and every other transition happen through ``lifecycle`` (builder / API).
Exports are limited to organizations the staff member may see.
"""

from __future__ import annotations

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from import_export import fields, resources
from import_export.admin import ExportMixin
from simple_history.admin import SimpleHistoryAdmin

from apps.core.resources import FormulaSafeExportMixin

from . import constants as c
from .answer_types import registry
from .constants import VersionStatus
from .metadata import clean_metadata
from .models import (
    ChecklistCategory,
    ChecklistItem,
    ChecklistOption,
    ChecklistRule,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistVersion,
)
from .rules import validate_rule_set
from .rules.snapshot import RuleSpec, snapshot_for_version


class TemplateExportResource(FormulaSafeExportMixin, resources.ModelResource):
    """Export-only: templates are created through the builder (services), never by file import."""

    organization = fields.Field(attribute="organization__code", column_name="organization")
    category = fields.Field(attribute="category__name", column_name="category")

    class Meta:
        model = ChecklistTemplate
        fields = ("organization", "code", "name", "category", "status", "description", "estimated_duration_minutes")
        export_order = fields

    def before_import(self, dataset, **kwargs):  # noqa: ANN001, ANN003
        raise PermissionError("Checklist templates cannot be imported here; use the builder or the items import.")


def _published(obj) -> bool:  # noqa: ANN001
    """Does ``obj`` (a version, or anything inside one) belong to a non-draft - published or superseded - version?"""
    version = obj if isinstance(obj, ChecklistVersion) else (
        getattr(obj, "version", None) or getattr(getattr(obj, "section", None), "version", None)
        or getattr(getattr(getattr(obj, "item", None), "section", None), "version", None)
    )
    return bool(version and version.status != VersionStatus.DRAFT)


class _FrozenWhenPublished:
    """Mixin: no change/delete of anything that belongs to a published version."""

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return not (obj is not None and _published(obj)) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return not (obj is not None and _published(obj)) and super().has_delete_permission(request, obj)


@admin.register(ChecklistCategory)
class ChecklistCategoryAdmin(SimpleHistoryAdmin):
    list_display = ("name", "organization", "sort_order")
    list_filter = ("organization",)
    search_fields = ("name",)
    autocomplete_fields = ("organization",)


class VersionInline(admin.TabularInline):
    model = ChecklistVersion
    extra = 0
    fields = ("version_number", "status", "change_summary", "published_at")
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


@admin.register(ChecklistTemplate)
class ChecklistTemplateAdmin(ExportMixin, SimpleHistoryAdmin):
    resource_classes = [TemplateExportResource]
    list_display = ("code", "name", "organization", "category", "status", "updated_at")
    list_filter = ("status", "organization", "category")
    search_fields = ("code", "name", "description")
    autocomplete_fields = ("organization",)
    readonly_fields = ("id", "slug", "created_by", "updated_by", "created_at", "updated_at")
    inlines = [VersionInline]

    def get_readonly_fields(self, request, obj=None):  # noqa: ANN001, ANN201
        fields_ = list(super().get_readonly_fields(request, obj))
        return fields_ + (["organization", "code"] if obj is not None else [])  # identity never changes

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False  # archive instead (later phases reference templates)

    def get_export_queryset(self, request):  # noqa: ANN001, ANN201
        """Exports start from the same authorized selector as every other channel."""
        from . import selectors

        return selectors.templates_for_user(request.user).order_by("organization__code", "code")


class SectionInline(admin.TabularInline):
    model = ChecklistSection
    extra = 0
    fields = ("sort_order", "name", "is_required")
    show_change_link = True

    def has_add_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return obj is not None and obj.status == VersionStatus.DRAFT

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return obj is not None and obj.status == VersionStatus.DRAFT

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return obj is not None and obj.status == VersionStatus.DRAFT


@admin.register(ChecklistVersion)
class ChecklistVersionAdmin(SimpleHistoryAdmin):
    list_display = ("__str__", "status", "published_at", "published_by", "superseded_at", "created_at")
    list_filter = ("status",)
    search_fields = ("template__code", "template__name")
    list_select_related = ("template", "published_by")
    readonly_fields = (
        "id", "template", "version_number", "status", "based_on", "published_at", "published_by", "superseded_at",
        "integrity_hash", "publication_report", "created_by", "created_at", "updated_at",
    )
    inlines = [SectionInline]

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False  # drafts are created by the lifecycle service (builder / API), never by a bare form

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False  # a draft is discarded through the lifecycle service; published versions are never deleted

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return not (obj is not None and obj.status != VersionStatus.DRAFT) and super().has_change_permission(request, obj)


class ItemAdminForm(forms.ModelForm):
    """The admin edits an item through the same answer-type rules as the builder (no way around them)."""

    class Meta:
        model = ChecklistItem
        fields = "__all__"

    def clean(self):  # noqa: ANN201
        cleaned = super().clean()
        answer_type = cleaned.get("answer_type")
        if answer_type:
            try:
                cleaned["config"] = c.clean_config(answer_type, cleaned.get("config"))
            except ValidationError as exc:
                self.add_error("config", exc.messages)
        try:
            cleaned["metadata"] = clean_metadata(cleaned.get("metadata"))
        except ValidationError as exc:
            self.add_error("metadata", exc.messages)
        return cleaned


class _FrozenInline:
    """Inline mixin: nothing inside a non-draft version can be added, changed or deleted through its parent's page."""

    def has_add_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return not (obj is not None and _published(obj)) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return not (obj is not None and _published(obj)) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return not (obj is not None and _published(obj)) and super().has_delete_permission(request, obj)


class ItemInline(_FrozenInline, admin.TabularInline):
    model = ChecklistItem
    extra = 0
    fields = ("sort_order", "code", "question", "answer_type", "is_required", "is_active")
    show_change_link = True


@admin.register(ChecklistSection)
class ChecklistSectionAdmin(_FrozenWhenPublished, SimpleHistoryAdmin):
    list_display = ("name", "version", "sort_order", "is_required")
    search_fields = ("name", "version__template__code")
    list_select_related = ("version__template",)
    inlines = [ItemInline]


class OptionInline(_FrozenInline, admin.TabularInline):
    model = ChecklistOption
    extra = 0
    fields = ("sort_order", "value", "label", "is_active")


@admin.register(ChecklistItem)
class ChecklistItemAdmin(_FrozenWhenPublished, SimpleHistoryAdmin):
    form = ItemAdminForm
    list_display = ("code", "question", "answer_type_label", "section", "is_required", "is_active")
    list_filter = ("answer_type", "is_required", "is_active")
    search_fields = ("code", "question", "section__version__template__code")
    list_select_related = ("section__version__template",)
    inlines = [OptionInline]
    readonly_fields = ("answer_type_summary",)

    @admin.display(description="Answer type", ordering="answer_type")
    def answer_type_label(self, obj) -> str:  # noqa: ANN001
        return registry.get(obj.answer_type).label if obj.answer_type in registry else obj.answer_type

    @admin.display(description="Answer type definition")
    def answer_type_summary(self, obj) -> str:  # noqa: ANN001
        if obj.answer_type not in registry:
            return "Unknown answer type"
        definition = registry.get(obj.answer_type)
        caps = ", ".join(k.replace("_", " ") for k, v in definition.capabilities.as_dict().items() if v)
        return f"{definition.label} - {definition.description} Capabilities: {caps or 'none'}."


@admin.register(ChecklistOption)
class ChecklistOptionAdmin(_FrozenWhenPublished, SimpleHistoryAdmin):
    list_display = ("label", "value", "item", "sort_order", "is_active")
    search_fields = ("label", "value", "item__code")


class RuleAdminForm(forms.ModelForm):
    """The admin saves a rule only if the whole rule set of its version still validates - the same gate as the builder and API.

    References, operators and values, actions, limits, dependency cycles and definite conflicts are all checked, and the
    stored condition/actions are replaced by their normalized form.  (A "rule" typed as raw JSON can therefore not bypass anything.)
    """

    class Meta:
        model = ChecklistRule
        fields = ("version", "code", "name", "description", "condition", "actions", "is_active", "sort_order")

    def clean(self):  # noqa: ANN201
        cleaned = super().clean()
        version = cleaned.get("version") or getattr(self.instance, "version", None)
        if version is None or any(f in self.errors for f in ("condition", "actions", "code", "name")):
            return cleaned
        if version.status != VersionStatus.DRAFT:
            raise ValidationError("Rules can only be added to or changed in a draft version.")
        code = cleaned.get("code") or self.instance.code
        snapshot = snapshot_for_version(version)
        others = [r for r in snapshot.rules if r.code != self.instance.code]
        candidate = RuleSpec(code, cleaned.get("name", ""), cleaned.get("condition"), cleaned.get("actions"),
                             bool(cleaned.get("is_active", True)), cleaned.get("sort_order") or 0, cleaned.get("description", ""))
        report = validate_rule_set(snapshot.with_rules([*others, candidate]))
        if report.errors:
            raise ValidationError([f"{i.path + ': ' if i.path else ''}{i.message}" for i in report.errors[:10]])
        compiled = next(r for r in report.compiled if r.code == code)
        cleaned["condition"], cleaned["actions"] = compiled.stored_condition, compiled.stored_actions
        return cleaned


@admin.register(ChecklistRule)
class ChecklistRuleAdmin(_FrozenWhenPublished, SimpleHistoryAdmin):
    form = RuleAdminForm
    list_display = ("code", "name", "version", "is_active", "sort_order")
    list_filter = ("is_active", "version__status")
    search_fields = ("code", "name", "version__template__code")
    list_select_related = ("version__template",)

    def get_readonly_fields(self, request, obj=None):  # noqa: ANN001, ANN201
        return ("version", "code") if obj is not None else ()  # identity never changes

    def formfield_for_foreignkey(self, db_field, request, **kwargs):  # noqa: ANN001, ANN003, ANN201
        if db_field.name == "version":  # rules are only ever added to a draft
            kwargs["queryset"] = ChecklistVersion.objects.filter(status=VersionStatus.DRAFT).select_related("template")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
