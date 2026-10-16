"""The Answer Type Engine (Phase 5): registry, configuration schemas, validation and the value contract.

Public surface::

    from apps.checklists.answer_types import registry, get_answer_type, validate_answer, AnswerContext

The engine defines *how an item is answered*; it stores no answers (execution, Phase 9), evaluates no rules
(Phase 6) and computes no checklist scores (Phase 15).
"""

from __future__ import annotations

from .base import ANSWERED, EMPTY, INVALID, NOT_APPLICABLE, AnswerContext, AnswerError, AnswerResult, OptionRef
from .exceptions import AnswerInvalid, ConfigurationError
from .registry import CATEGORIES, get_answer_type, get_answer_type_definitions, registry
from .validators import context_for_item, resolve_option, validate_answer, validate_in_context

__all__ = [
    "ANSWERED", "CATEGORIES", "EMPTY", "INVALID", "NOT_APPLICABLE", "AnswerContext", "AnswerError", "AnswerInvalid",
    "AnswerResult", "ConfigurationError", "OptionRef", "context_for_item", "get_answer_type",
    "get_answer_type_definitions", "registry", "resolve_option", "validate_answer", "validate_in_context",
]
