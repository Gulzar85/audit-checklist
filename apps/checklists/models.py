"""Checklist definition models (Phase 4).

    ChecklistTemplate -> ChecklistVersion -> ChecklistSection -> ChecklistItem -> ChecklistOption
                                   \\-> ChecklistRule

A *template* is the stable identity of a checklist; a *version* is a snapshot of its structure.  Sections
hang off a version, never off the template, so changing a later version can never rewrite an earlier one.
Future executions will reference a ``ChecklistVersion``.  There is exactly one item model: what is asked
is ``question`` and how it is answered is ``answer_type`` + ``config`` + options - nothing here assumes
scoring, and no answer is stored in this app.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel
from apps.locations.models import normalize_code, validate_code

from .constants import AnswerType, RuleAction, RuleOperator, TemplateStatus, VersionStatus

User = settings.AUTH_USER_MODEL

_DEFERRED = models.Deferrable.DEFERRED  # lets a reorder swap sort_order values inside one transaction


class ChecklistCategory(BaseModel):
    """An organization-defined grouping (``Food Safety``, ``Maintenance`` ...).  No built-in categories."""

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="checklist_categories")
    name = models.CharField(max_length=80)
    sort_order = models.PositiveIntegerField(default=0)

    history = HistoricalRecords()

    class Meta:
        ordering = ("sort_order", "name")
        verbose_name_plural = "checklist categories"
        constraints = [
            models.UniqueConstraint(Lower("name"), "organization", name="unique_checklist_category_per_organization"),
        ]

    def __str__(self) -> str:
        return self.name


class ChecklistTemplate(BaseModel):
    """The logical definition of a checklist, owned by one organization."""

    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="checklist_templates")
    category = models.ForeignKey(ChecklistCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="templates")
    name = models.CharField(max_length=200)
    #: Stable human-readable identifier (``FS-001``); never derived from the name after creation.
    code = models.CharField(max_length=40)
    slug = models.SlugField(max_length=80)
    description = models.TextField(blank=True)
    instructions = models.TextField(blank=True, help_text=_("General guidance shown before the checklist starts."))
    estimated_duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    #: The one authoritative lifecycle field (no parallel is_active / is_published flags).
    status = models.CharField(max_length=10, choices=TemplateStatus.choices, default=TemplateStatus.DRAFT, db_index=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    history = HistoricalRecords()

    class Meta:
        ordering = ("name", "code")
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="unique_checklist_code_per_organization"),
            models.UniqueConstraint(fields=["organization", "slug"], name="unique_checklist_slug_per_organization"),
            models.CheckConstraint(condition=models.Q(status__in=["draft", "active", "archived"]), name="checklist_template_status_valid"),
        ]
        indexes = [models.Index(fields=["organization", "status"])]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("checklists:detail", args=[self.pk])

    def clean_fields(self, exclude=None) -> None:  # noqa: ANN001
        self.code = normalize_code(self.code)
        super().clean_fields(exclude=exclude)
        if self.code:
            validate_code(self.code)
        if self.category_id and self.category.organization_id != self.organization_id:
            raise ValidationError({"category": _("The category belongs to another organization.")})


class ChecklistVersion(BaseModel):
    """A snapshot of a template's structure.  Only a draft version may be edited."""

    template = models.ForeignKey(ChecklistTemplate, on_delete=models.PROTECT, related_name="versions")
    version_number = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=VersionStatus.choices, default=VersionStatus.DRAFT, db_index=True)
    change_summary = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ("-version_number",)
        constraints = [
            models.UniqueConstraint(fields=["template", "version_number"], name="unique_checklist_version_number"),
            models.UniqueConstraint(
                fields=["template"], condition=models.Q(status="draft"), name="one_draft_version_per_template"
            ),
            models.CheckConstraint(
                condition=models.Q(status="published", published_at__isnull=False)
                | models.Q(status="draft", published_at__isnull=True),
                name="checklist_version_status_matches_published_at",
            ),
            models.CheckConstraint(condition=models.Q(version_number__gte=1), name="checklist_version_number_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.template.code} v{self.version_number}"

    @property
    def is_draft(self) -> bool:
        return self.status == VersionStatus.DRAFT


class ChecklistSection(BaseModel):
    """A titled group of items.  Flat on purpose (no nesting); repeatable sections can be added later."""

    version = models.ForeignKey(ChecklistVersion, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    instructions = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField()
    is_required = models.BooleanField(default=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ("sort_order",)
        constraints = [
            models.UniqueConstraint(fields=["version", "sort_order"], name="unique_section_order_per_version", deferrable=_DEFERRED),
        ]

    def __str__(self) -> str:
        return self.name


class ChecklistItem(BaseModel):
    """One question / check / control point.  The single item model for every answer type."""

    section = models.ForeignKey(ChecklistSection, on_delete=models.CASCADE, related_name="items")
    #: Stable identifier (``TEMP-001``) for reports, APIs, imports and version comparison.
    code = models.CharField(max_length=40)
    question = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    help_text = models.CharField(max_length=500, blank=True)
    instructions = models.TextField(blank=True)
    answer_type = models.CharField(max_length=20, choices=AnswerType.choices, default=AnswerType.YES_NO)
    #: Type-specific settings (score range, numeric bounds ...); validated by ``constants.clean_config``.
    config = models.JSONField(default=dict, blank=True)
    #: Must be answered *when the item is applicable* (conditional applicability arrives with the rules engine).
    is_required = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField()

    history = HistoricalRecords()

    class Meta:
        ordering = ("sort_order",)
        constraints = [
            models.UniqueConstraint(fields=["section", "sort_order"], name="unique_item_order_per_section", deferrable=_DEFERRED),
            models.UniqueConstraint(fields=["section", "code"], name="unique_item_code_per_section"),
            models.CheckConstraint(
                condition=models.Q(answer_type__in=[c for c, _label in AnswerType.choices]), name="checklist_item_answer_type_valid"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.question[:50]}"


class ChecklistOption(BaseModel):
    """A predefined choice of an item.  No score lives here: scoring is a later, independent capability."""

    item = models.ForeignKey(ChecklistItem, on_delete=models.CASCADE, related_name="options")
    value = models.CharField(max_length=64, help_text=_("Stable machine value (kept when the label changes)."))
    label = models.CharField(max_length=200)
    sort_order = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ("sort_order",)
        constraints = [
            models.UniqueConstraint(fields=["item", "sort_order"], name="unique_option_order_per_item", deferrable=_DEFERRED),
            models.UniqueConstraint(fields=["item", "value"], name="unique_option_value_per_item"),
        ]

    def __str__(self) -> str:
        return self.label


class ChecklistRule(BaseModel):
    """A generic *if source-item <operator> value then <action> [target]* definition (evaluated in Phase 6)."""

    version = models.ForeignKey(ChecklistVersion, on_delete=models.CASCADE, related_name="rules")
    source_item = models.ForeignKey(ChecklistItem, on_delete=models.CASCADE, related_name="rules")
    operator = models.CharField(max_length=12, choices=RuleOperator.choices)
    value = models.JSONField(null=True, blank=True)
    action = models.CharField(max_length=20, choices=RuleAction.choices)
    target_item = models.ForeignKey(ChecklistItem, on_delete=models.CASCADE, null=True, blank=True, related_name="targeted_by_rules")
    target_section = models.ForeignKey(ChecklistSection, on_delete=models.CASCADE, null=True, blank=True, related_name="targeted_by_rules")
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ("sort_order", "created_at")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(target_item__isnull=True) | models.Q(target_section__isnull=True),
                name="checklist_rule_single_target",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source_item_id} {self.operator} -> {self.action}"
