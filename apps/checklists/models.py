"""Checklist definition models (Phase 4).

    ChecklistTemplate -> ChecklistVersion -> ChecklistSection -> ChecklistItem -> ChecklistOption
                                   \\-> ChecklistRule

A *template* is the stable identity of a checklist; a *version* is a snapshot of its structure.  Sections
hang off a version, never off the template, so changing a later version can never rewrite an earlier one.  A version
moves draft -> published -> superseded (``lifecycle.py``); everything below a non-draft version is immutable (``guards.py``:
model guards here, PostgreSQL triggers underneath).
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

from .constants import AnswerType, TemplateStatus, VersionStatus
from .guards import DefinitionGuardMixin, definition_changes_allowed

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
    """A reproducible definition of a template.  Only a draft may be edited; see ``lifecycle.py`` for the transitions."""

    template = models.ForeignKey(ChecklistTemplate, on_delete=models.PROTECT, related_name="versions")
    version_number = models.PositiveIntegerField()
    #: The one authoritative lifecycle field: draft -> published -> superseded (no parallel is_published / is_current flags).
    status = models.CharField(max_length=10, choices=VersionStatus.choices, default=VersionStatus.DRAFT, db_index=True)
    change_summary = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    #: The version this one was copied from (None for the first version): "draft cloned from v1", rollback lineage.
    based_on = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="derived_versions")
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    #: Set when a newer version is published (the version stays readable and immutable).
    superseded_at = models.DateTimeField(null=True, blank=True)
    #: ``sha256:v<schema>:<hex>`` of the canonical manifest, sealed at publication (``manifest.py``); never trusted without re-derivation.
    integrity_hash = models.CharField(max_length=80, blank=True)
    #: What the publication validator found (warnings accepted at publication, counts); frozen with the version.
    publication_report = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ("-version_number",)
        constraints = [
            models.UniqueConstraint(fields=["template", "version_number"], name="unique_checklist_version_number"),
            models.UniqueConstraint(
                fields=["template"], condition=models.Q(status="draft"), name="one_draft_version_per_template"
            ),
            models.UniqueConstraint(
                fields=["template"], condition=models.Q(status="published"), name="one_published_version_per_template"
            ),
            models.CheckConstraint(
                condition=models.Q(status="draft", published_at__isnull=True, superseded_at__isnull=True)
                | models.Q(status="published", published_at__isnull=False, superseded_at__isnull=True)
                | models.Q(status="superseded", published_at__isnull=False, superseded_at__isnull=False),
                name="checklist_version_lifecycle_consistent",
            ),
            models.CheckConstraint(condition=models.Q(version_number__gte=1), name="checklist_version_number_positive"),
            models.CheckConstraint(condition=~models.Q(based_on=models.F("id")), name="checklist_version_not_based_on_itself"),
        ]

    def __str__(self) -> str:
        return f"{self.template.code} v{self.version_number}"

    @property
    def is_draft(self) -> bool:
        return self.status == VersionStatus.DRAFT

    @property
    def is_published(self) -> bool:
        return self.status == VersionStatus.PUBLISHED

    @property
    def is_superseded(self) -> bool:
        return self.status == VersionStatus.SUPERSEDED

    def _stored_status(self) -> str | None:
        return type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()

    def save(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        """A stored version moves draft -> published -> superseded only; nothing else may rewrite it (ADR 0049)."""
        if not self._state.adding and not definition_changes_allowed():
            old = self._stored_status()
            if old is not None and old != VersionStatus.DRAFT and not (old == VersionStatus.PUBLISHED and self.status == VersionStatus.SUPERSEDED):
                raise ValidationError(
                    _("This checklist version is already published and cannot be edited. Create a new draft version to make changes."),
                    code="version_immutable",
                )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        if not definition_changes_allowed() and self._stored_status() not in (None, VersionStatus.DRAFT):
            raise ValidationError(_("A published checklist version cannot be deleted; it is kept for history."), code="version_immutable")
        return super().delete(*args, **kwargs)


class ChecklistSection(DefinitionGuardMixin, BaseModel):
    """A titled group of items.  Flat on purpose (no nesting); repeatable sections can be added later."""

    version = models.ForeignKey(ChecklistVersion, on_delete=models.CASCADE, related_name="sections")
    #: Stable identifier within the version (``S-001``): rules target sections by code, and the code survives
    #: copying to a new version, so rule references never need remapping.
    code = models.CharField(max_length=40)
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
            models.UniqueConstraint(fields=["version", "code"], name="unique_section_code_per_version"),
        ]

    def __str__(self) -> str:
        return self.name


class ChecklistItem(DefinitionGuardMixin, BaseModel):
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
    #: Descriptive facts about the item that the *answer engine ignores* (reference points, critical flag,
    #: applicability tags, source reference).  Validated by ``apps.checklists.metadata``; read by the later
    #: scoring / rules phases, so source workbooks can be imported without losing them.
    metadata = models.JSONField(default=dict, blank=True)

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


class ChecklistOption(DefinitionGuardMixin, BaseModel):
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


class ChecklistRule(DefinitionGuardMixin, BaseModel):
    """A declarative rule: *when* ``condition`` holds, apply ``actions`` (Phase 6).

    Items and sections are referenced **by code** inside the JSON, so a rule survives copying to a new version
    unchanged.  The JSON is never trusted: ``apps.checklists.rules`` validates it against the version's items, option
    lists and answer types on every save, on publish and on import, and the evaluator only interprets the
    registries' operators and actions - there is no expression language and nothing is executed.
    """

    version = models.ForeignKey(ChecklistVersion, on_delete=models.CASCADE, related_name="rules")
    #: Stable identifier within the version (``R-001``).
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True)
    #: Condition tree (``{"operator": "AND"|"OR", "conditions": [...]}``); ``null`` = always applies.
    condition = models.JSONField(null=True, blank=True)
    #: List of action objects (``{"type": "SHOW_ITEM", "target": {...}, ...}``).
    actions = models.JSONField(default=list)
    #: Evaluation order (also the display order); contiguous, starting at 1.
    sort_order = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ("sort_order",)
        constraints = [
            models.UniqueConstraint(fields=["version", "code"], name="unique_rule_code_per_version"),
            models.UniqueConstraint(fields=["version", "sort_order"], name="unique_rule_order_per_version", deferrable=_DEFERRED),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"
