"""Scoring models (Phase 15).

* ``ScoringConfig`` - the scoring configuration of exactly one checklist version (scalar settings + one validated JSON document keyed by
  stable question / section codes and option values).  It is editable only while its version is a **draft**; once the version is published
  the row is frozen by a database trigger (``guards.py``) and is part of the version's integrity hash.
* ``ScoreResult`` - an append-only snapshot of a score: the exact version and configuration it was computed under, the inputs'
  fingerprint, the numbers, the grade and the full breakdown.  Rows are never updated (except retiring the *current* flag when an authorized
  recalculation appends a successor) and never deleted.

One configuration table instead of one table per concept (rules, weights, bands, critical rules) is deliberate: a configuration is always
read, validated, sealed, cloned and compared as a whole, so a single immutable row keeps the version's seal simple and
impossible to half-apply (ADR 0073).  The document is declarative and strictly validated; nothing in it is executable.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel, UUIDModel

from . import constants as k
from .config import config_hash, default_settings, empty_document

User = settings.AUTH_USER_MODEL


class ScoringConfig(BaseModel):
    version = models.OneToOneField("checklists.ChecklistVersion", on_delete=models.PROTECT, related_name="scoring_config")
    #: Denormalized from the version's template for scoping; the service always sets it from the version.
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="scoring_configs")
    enabled = models.BooleanField(default=True, help_text="Off: the version is published without a score (never a score of zero).")
    method = models.CharField(max_length=24, choices=k.Method.choices, default=k.Method.PERCENTAGE)
    na_policy = models.CharField(max_length=12, choices=k.NaPolicy.choices, default=k.NaPolicy.EXCLUDE)
    missing_policy = models.CharField(max_length=12, choices=k.MissingPolicy.choices, default=k.MissingPolicy.ZERO)
    precision = models.PositiveSmallIntegerField(default=2, help_text="Decimal places of the percentage (0-4).")
    critical_overrides_grade = models.BooleanField(default=False)
    critical_label = models.CharField(max_length=100, blank=True)
    #: ``{"items": {code: rule}, "sections": {code: weight}, "bands": [...], "critical": [...]}`` - see ``config.py``.
    document = models.JSONField(default=empty_document, blank=True)
    notes = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(precision__gte=0, precision__lte=4), name="scoring_config_precision_range"),
            models.CheckConstraint(condition=models.Q(method__in=k.Method.values), name="scoring_config_method_valid"),
            models.CheckConstraint(condition=models.Q(na_policy__in=k.NaPolicy.values), name="scoring_config_na_policy_valid"),
            models.CheckConstraint(condition=models.Q(missing_policy__in=k.MissingPolicy.values), name="scoring_config_missing_policy_valid"),
        ]

    def __str__(self) -> str:
        return f"Scoring of {self.version_id}"

    @property
    def settings(self) -> dict[str, object]:
        return {"enabled": self.enabled, "method": self.method, "na_policy": self.na_policy, "missing_policy": self.missing_policy,
                "precision": self.precision, "critical_overrides_grade": self.critical_overrides_grade, "critical_label": self.critical_label}

    @property
    def content_hash(self) -> str:
        return config_hash(self.settings, self.document)

    @staticmethod
    def defaults() -> dict[str, object]:
        return default_settings()


class ScoreResult(UUIDModel):
    """An immutable score snapshot of one execution (see the module docstring).  Deliberately not a ``BaseModel``: no ``updated_at``."""

    created_at = models.DateTimeField(auto_now_add=True)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="score_results")
    execution = models.ForeignKey("executions.ChecklistExecution", on_delete=models.PROTECT, related_name="score_results")
    version = models.ForeignKey("checklists.ChecklistVersion", on_delete=models.PROTECT, related_name="score_results")
    config = models.ForeignKey(ScoringConfig, on_delete=models.PROTECT, related_name="results")
    config_hash = models.CharField(max_length=80)
    algorithm = models.PositiveSmallIntegerField(default=k.ALGORITHM_VERSION)
    status = models.CharField(max_length=16, choices=k.ResultStatus.choices)
    kind = models.CharField(max_length=16, choices=k.ResultKind.choices, default=k.ResultKind.SUBMISSION)
    #: Exactly one current result per execution; an authorized recalculation appends a new one and retires the previous.
    is_current = models.BooleanField(default=True)
    method = models.CharField(max_length=24)
    earned = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    available = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    percentage = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    grade = models.CharField(max_length=20, blank=True)
    label = models.CharField(max_length=100, blank=True)
    display_label = models.CharField(max_length=100, blank=True)
    critical = models.BooleanField(default=False)
    breakdown = models.JSONField(default=dict, blank=True)
    execution_revision = models.PositiveIntegerField(default=0)
    input_fingerprint = models.CharField(max_length=64)
    reason = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["execution"], condition=models.Q(is_current=True), name="score_result_one_current_per_execution"),
            models.CheckConstraint(condition=models.Q(status__in=k.ResultStatus.values), name="score_result_status_valid"),
            models.CheckConstraint(condition=models.Q(percentage__isnull=True) | models.Q(percentage__gte=0, percentage__lte=100), name="score_result_percentage_range"),
            models.CheckConstraint(
                condition=models.Q(status="not_applicable", percentage__isnull=True) | ~models.Q(status="not_applicable"), name="score_result_na_has_no_percentage"),
        ]
        indexes = [models.Index(fields=["organization", "status", "-created_at"], name="score_result_org_status_idx"),
                   models.Index(fields=["version", "is_current"], name="score_result_version_idx")]

    def __str__(self) -> str:
        return f"{self.execution_id}: {self.status} {self.percentage}"
