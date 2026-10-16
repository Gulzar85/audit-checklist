"""The answer validation service: one entry point for web, API, imports and (later) execution and offline sync.

``validate_answer(item, raw)`` answers: *is this a valid answer for this item, and what is its normalized value?*
It distinguishes the three states the spec calls for - **required**, **optional** and **not applicable** - and never
treats an empty value as automatically invalid:

* ``required`` item + empty value + not N/A  -> invalid (``required``);
* optional item + empty value                -> valid, status ``empty``, no value;
* ``not_applicable=True``                    -> valid, status ``not_applicable`` (whether N/A is *permitted* for a
  given item is a rule decision - Phase 6 - and is made by the caller before it passes the flag);
* a value                                    -> validated by the answer type and normalized.

The ``required`` argument lets the later conditional-rules engine override an item's own flag per execution.

Options are validated against the item's *own* options only, so a submitted option id/value from another item,
checklist version or organization simply does not resolve.  Pass ``organization`` and/or ``version`` to also
assert that the item belongs where the caller says it does (defence against forged item ids).
"""

from __future__ import annotations

from typing import Any

from .base import (
    ANSWERED,
    EMPTY,
    INVALID,
    NOT_APPLICABLE,
    AnswerContext,
    AnswerError,
    AnswerResult,
    OptionRef,
)
from .exceptions import AnswerInvalid
from .registry import registry


def context_for_item(item: Any, *, required: bool | None = None) -> AnswerContext:
    """Build the DB-free validation context of a checklist item (uses prefetched options when available)."""
    options: tuple[OptionRef, ...] = ()
    if item.answer_type in registry and registry.get(item.answer_type).capabilities.supports_options:
        options = tuple(
            OptionRef(id=str(o.pk), value=o.value, label=o.label, sort_order=o.sort_order)
            for o in item.options.all() if o.is_active
        )
    definition = registry.get(item.answer_type) if item.answer_type in registry else None
    return AnswerContext(
        answer_type=item.answer_type,
        config=definition.config_with_defaults(item.config) if definition else {},
        options=options,
        required=item.is_required if required is None else required,
        item_id=str(item.pk),
    )


def validate_in_context(ctx: AnswerContext, raw: Any, *, not_applicable: bool = False) -> AnswerResult:
    """Validate ``raw`` against a context (pure; no database)."""
    if ctx.answer_type not in registry:
        return _invalid("unknown_type", "This question uses an answer type that is not available.")
    definition = registry.get(ctx.answer_type)
    if not_applicable:
        return AnswerResult(NOT_APPLICABLE)
    if definition.is_empty(raw):
        if ctx.required:
            return _invalid("required", "An answer is required.")
        return AnswerResult(EMPTY)
    try:
        value = definition.normalize(raw, ctx)
    except AnswerInvalid as exc:
        return AnswerResult(INVALID, errors=(AnswerError(exc.code, exc.message, exc.path),))
    return AnswerResult(ANSWERED, value=value, serialized=definition.serialize(value))


def validate_answer(
    item: Any, raw: Any, *, required: bool | None = None, not_applicable: bool = False,
    organization: Any = None, version: Any = None,
) -> AnswerResult:
    """Validate an answer to a :class:`~apps.checklists.models.ChecklistItem`."""
    scope_error = _scope_error(item, organization, version)
    if scope_error:
        return scope_error
    if not item.is_active:
        return _invalid("item_inactive", "This question is no longer active.")
    return validate_in_context(context_for_item(item, required=required), raw, not_applicable=not_applicable)


def resolve_option(item: Any, reference: Any, *, organization: Any = None, version: Any = None) -> str | None:
    """The option *value* a reference (value or id) denotes for ``item``, or ``None`` - never another item's option."""
    if _scope_error(item, organization, version) or not isinstance(reference, str):
        return None
    for option in item.options.all():
        if option.is_active and reference in (option.value, str(option.pk)):
            return option.value
    return None


def _scope_error(item: Any, organization: Any, version: Any) -> AnswerResult | None:
    if version is not None and item.section.version_id != getattr(version, "pk", version):
        return _invalid("scope", "This question does not belong to that checklist version.")
    if organization is not None:
        expected = getattr(organization, "pk", organization)
        if item.section.version.template.organization_id != expected:
            return _invalid("scope", "This question does not belong to that organization.")
    return None


def _invalid(code: str, message: str) -> AnswerResult:
    return AnswerResult(INVALID, errors=(AnswerError(code, message),))
