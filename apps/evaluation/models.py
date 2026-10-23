"""Evaluation models (Phase 16).

* ``EvaluationRuleSet`` - the rules of exactly one checklist version (the same attachment as scoring): editable only while the version is a
  draft, frozen by a database trigger and sealed in the version's integrity hash once it is published.  Its lifecycle *is* the version's:
  draft -> published -> superseded (retired, kept for history).  There is deliberately no organization or global inheritance: nothing implicit
  overrides anything, and the rule set an execution is evaluated with is always the one of its own version (ADR 0076).
* ``EvaluationRun`` - an append-only record of one evaluation: the exact rule-set version and hash, the engine version, the inputs'
  fingerprint, the outcome and the full per-rule / per-condition detail with explanations.  Never updated (except retiring ``is_current``).
* ``EvaluationActionRequest`` - one action a matched rule asked for, with its own outcome (distinct from the evaluation).  Only ``pending`` rows
  can change, and a *succeeded* ``action_key`` is unique per organization, so re-evaluating never repeats a side effect.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel, UUIDModel

from . import constants as k
from .config import empty_document, ruleset_hash

User = settings.AUTH_USER_MODEL


class EvaluationRuleSet(BaseModel):
    version = models.OneToOneField("checklists.ChecklistVersion", on_delete=models.PROTECT, related_name="evaluation_ruleset")
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="evaluation_rulesets")
    enabled = models.BooleanField(default=True, help_text="Off: the version is published without automated evaluation.")
    #: ``{"rules": [...]}`` - see ``config.py``.  Declarative and strictly validated; never executed as code.
    document = models.JSONField(default=empty_document, blank=True)
    notes = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    def __str__(self) -> str:
        return f"Evaluation rules of {self.version_id}"

    @property
    def content_hash(self) -> str:
        return ruleset_hash(self.enabled, self.document)


class EvaluationRun(UUIDModel):
    created_at = models.DateTimeField(auto_now_add=True)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="evaluation_runs")
    execution = models.ForeignKey("executions.ChecklistExecution", on_delete=models.PROTECT, related_name="evaluation_runs")
    version = models.ForeignKey("checklists.ChecklistVersion", on_delete=models.PROTECT, related_name="evaluation_runs")
    ruleset = models.ForeignKey(EvaluationRuleSet, on_delete=models.PROTECT, related_name="runs")
    ruleset_hash = models.CharField(max_length=80)
    engine_version = models.PositiveSmallIntegerField(default=k.ENGINE_VERSION)
    trigger = models.CharField(max_length=16, choices=k.Trigger.choices)
    outcome = models.CharField(max_length=16, choices=k.Outcome.choices)
    #: Exactly one current run per execution; a re-evaluation with changed inputs or rules appends a new one and retires the previous.
    is_current = models.BooleanField(default=True)
    execution_revision = models.PositiveIntegerField(default=0)
    score_result = models.ForeignKey("scoring.ScoreResult", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    input_fingerprint = models.CharField(max_length=64)
    #: sha256(execution, rule-set hash, engine version, inputs fingerprint, execution revision): the same inputs never create a second run.
    idempotency_key = models.CharField(max_length=64)
    reason = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")  # null = the system (submission)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["execution"], condition=models.Q(is_current=True), name="evaluation_run_one_current_per_execution"),
            models.UniqueConstraint(fields=["idempotency_key"], name="evaluation_run_unique_key"),
            models.CheckConstraint(condition=models.Q(outcome__in=k.Outcome.values), name="evaluation_run_outcome_valid"),
            models.CheckConstraint(condition=models.Q(trigger__in=k.Trigger.values), name="evaluation_run_trigger_valid"),
        ]
        indexes = [models.Index(fields=["organization", "outcome", "-created_at"], name="eval_run_org_outcome_idx"),
                   models.Index(fields=["version", "is_current"], name="eval_run_version_idx")]

    def __str__(self) -> str:
        return f"{self.execution_id}: {self.outcome}"


class EvaluationActionRequest(UUIDModel):
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="evaluation_action_requests")
    run = models.ForeignKey(EvaluationRun, on_delete=models.PROTECT, related_name="actions")
    execution = models.ForeignKey("executions.ChecklistExecution", on_delete=models.PROTECT, related_name="evaluation_action_requests")
    rule_code = models.CharField(max_length=40)
    action_index = models.PositiveSmallIntegerField()
    action_type = models.CharField(max_length=24, choices=k.ActionType.choices)
    #: ``eval:<execution>:<rule>:<index>`` - the identity of the side effect across runs.
    action_key = models.CharField(max_length=200)
    params = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=k.ActionStatus.choices, default=k.ActionStatus.PENDING)
    result_type = models.CharField(max_length=24, blank=True)
    result_id = models.CharField(max_length=64, blank=True)
    message = models.CharField(max_length=500, blank=True)
    error = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["run_id", "rule_code", "action_index"]
        constraints = [
            models.UniqueConstraint(fields=["run", "rule_code", "action_index"], name="evaluation_action_unique_per_run"),
            models.UniqueConstraint(fields=["organization", "action_key"], condition=models.Q(status="succeeded"), name="evaluation_action_succeeded_once"),
            models.CheckConstraint(condition=models.Q(status__in=k.ActionStatus.values), name="evaluation_action_status_valid"),
            models.CheckConstraint(condition=models.Q(action_type__in=k.ActionType.values), name="evaluation_action_type_valid"),
        ]
        indexes = [models.Index(fields=["organization", "status"], name="eval_action_org_status_idx")]

    def __str__(self) -> str:
        return f"{self.rule_code}#{self.action_index} {self.action_type}: {self.status}"

