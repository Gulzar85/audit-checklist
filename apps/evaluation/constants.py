"""Vocabularies of the evaluation engine (Phase 16).  Everything a rule may say comes from here - nothing is free text or code."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

#: Bumped when evaluation semantics change in a way that can alter a result; stored on every run (history stays interpretable).
ENGINE_VERSION = 1


class Outcome(models.TextChoices):
    PASS = "pass", _("Pass")
    WARNING = "warning", _("Warning")
    REVIEW_REQUIRED = "review_required", _("Review required")
    FAIL = "fail", _("Fail")
    INDETERMINATE = "indeterminate", _("Indeterminate")
    NOT_APPLICABLE = "not_applicable", _("Not applicable")


#: What a *rule* may produce when it matches.  INDETERMINATE only arises from an unknown condition (``on_unknown``); NOT_APPLICABLE only
#: when the version has no enabled rule.
RULE_OUTCOMES = (Outcome.PASS, Outcome.WARNING, Outcome.REVIEW_REQUIRED, Outcome.FAIL)

#: Conflict resolution: when several rules match, the highest precedence wins and *every* match stays listed - a critical result is never discarded.
PRECEDENCE = {Outcome.PASS: 0, Outcome.WARNING: 1, Outcome.INDETERMINATE: 2, Outcome.REVIEW_REQUIRED: 3, Outcome.FAIL: 4}


class OnUnknown(models.TextChoices):
    SKIP = "skip", _("Treat as not matched (recorded)")
    INDETERMINATE = "indeterminate", _("Make the evaluation indeterminate")


class RuleStatus(models.TextChoices):
    MATCHED = "matched", _("Matched")
    UNMATCHED = "unmatched", _("Not matched")
    UNKNOWN = "unknown", _("Could not be decided")
    SKIPPED = "skipped", _("Skipped")
    DISABLED = "disabled", _("Disabled")


class Trigger(models.TextChoices):
    SUBMISSION = "submission", _("Execution submitted")
    MANUAL = "manual", _("Authorized evaluation")
    REEVALUATION = "reevaluation", _("Reevaluation")
    SCORE = "score", _("Score recalculated")


class ActionType(models.TextChoices):
    REQUEST_FINDING = "request_finding", _("Request a finding")
    START_WORKFLOW = "start_workflow", _("Start a workflow")
    TAG = "tag", _("Record a tag")


class ActionStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    SUCCEEDED = "succeeded", _("Succeeded")
    SKIPPED = "skipped", _("Skipped (already done)")
    FAILED = "failed", _("Failed")


#: Input sources (see ``sources.py``).  ``answer`` is the Phase 6 answer state of a question of the version.
SOURCES = ("answer", "score", "execution")

MAX_RULES = 50
MAX_CONDITIONS = 25
MAX_DEPTH = 4  # root group > group > NOT > comparison: what the builder can express
MAX_ACTIONS = 5
MAX_MESSAGE = 300
MAX_TAG = 40
