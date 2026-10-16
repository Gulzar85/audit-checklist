"""The answer-type contract.

An *answer type* is behaviour defined in code: its metadata, the schema of its configuration, how a raw value is
validated and normalized, and how the normalized value is serialized.  Nothing about a type is stored per type in the
database - an item stores only ``answer_type`` (a code) and ``config`` (validated by the type's schema).

The engine is deliberately free of ORM access: :class:`AnswerContext` carries everything a validation needs (the
type, its configuration, the item's options), so the same code runs on the server for web and API requests and is
the reference for the PWA's local validation and the later offline-sync re-validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from .capabilities import Capabilities
from .exceptions import AnswerInvalid, ConfigurationError
from .schemas import ConfigField, clean_with_schema, jsonable

#: Answer statuses.  ``empty`` = an optional item left unanswered; ``not_applicable`` = explicitly N/A.
ANSWERED, EMPTY, NOT_APPLICABLE, INVALID = "answered", "empty", "not_applicable", "invalid"


@dataclass(frozen=True)
class OptionRef:
    """A selectable option of the item being answered (already scoped to that item by the caller)."""

    id: str
    value: str
    label: str
    sort_order: int = 0


@dataclass(frozen=True)
class AnswerContext:
    """Everything needed to validate one answer; no database access happens past this point."""

    answer_type: str
    config: dict[str, Any] = field(default_factory=dict)
    options: tuple[OptionRef, ...] = ()
    required: bool = True
    item_id: str = ""


@dataclass(frozen=True)
class AnswerError:
    code: str
    message: str
    path: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, **({"path": self.path} if self.path else {})}


@dataclass(frozen=True)
class AnswerResult:
    """The outcome of validating one answer."""

    status: str
    value: Any = None
    serialized: Any = None
    errors: tuple[AnswerError, ...] = ()

    @property
    def valid(self) -> bool:
        return self.status != INVALID

    @property
    def answered(self) -> bool:
        return self.status == ANSWERED

    def error_messages(self) -> list[str]:
        return [e.message for e in self.errors]

    def as_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "status": self.status, "value": self.serialized,
                "errors": [e.as_dict() for e in self.errors]}


class AnswerTypeDefinition:
    """Base class; subclasses set the class attributes and implement :meth:`normalize` / :meth:`serialize`."""

    code: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: Builder grouping: choice | value | datetime | media | advanced.
    category: ClassVar[str] = "value"
    icon: ClassVar[str] = "circle"
    capabilities: ClassVar[Capabilities] = Capabilities()
    config_fields: ClassVar[tuple[ConfigField, ...]] = ()
    #: How the builder previews the type (a key understood by ``preview.html``).
    preview: ClassVar[str] = "text"
    #: Documented example of a normalized value (API metadata and docs).
    example: ClassVar[Any] = None
    #: How rules may compare answers of this type (Phase 6): ``equality`` (boolean / yes-no), ``choice`` (one
    #: option), ``multi_choice``, ``ordered`` (numbers, scores, dates), ``text`` or ``presence`` only.  The rule
    #: operator registry is keyed by family, so a new answer type needs no rule-engine change.
    comparison_family: ClassVar[str] = "presence"
    #: Configuration keys a *rule* may tighten (``SET_VALIDATION_CONSTRAINT``); never loosen.
    rule_constraints: ClassVar[tuple[str, ...]] = ()

    # -- configuration ------------------------------------------------------------------------
    def clean_config(self, raw: Any) -> dict[str, Any]:
        """Validate ``raw`` and return the *storable* normalized configuration (defaults filled)."""
        cleaned = clean_with_schema(self.config_fields, raw)
        self.check_config(cleaned)
        return jsonable(cleaned)

    def check_config(self, cleaned: dict[str, Any]) -> None:
        """Cross-field rules (``min <= max`` ...); raise :class:`ConfigurationError`."""

    def defaults(self) -> dict[str, Any]:
        """The schema defaults (what a new item of this type starts with; may be incomplete, e.g. matrix rows)."""
        return jsonable({f.key: f.default for f in self.config_fields if f.default is not None})

    def config_with_defaults(self, stored: Any) -> dict[str, Any]:
        """Read a stored configuration tolerantly (older rows may lack newer keys): fill defaults, never fail."""
        try:
            return self.clean_config(stored)
        except ConfigurationError:
            return self.defaults()

    # -- answering ----------------------------------------------------------------------------
    def is_empty(self, raw: Any) -> bool:
        if raw is None:
            return True
        if isinstance(raw, str):
            return not raw.strip()
        if isinstance(raw, (list, tuple, dict, set)):
            return len(raw) == 0
        return False

    def normalize(self, raw: Any, ctx: AnswerContext) -> Any:
        """Return the normalized value or raise :class:`AnswerInvalid`.  Never called with an empty value."""
        raise NotImplementedError

    def parse_operand(self, raw: Any, ctx: AnswerContext) -> Any:
        """Parse a rule's *comparison value* with this type's own parsing rules (type, precision, format) but
        **without** the item's range/length constraints - "temperature greater than 25" is a legitimate rule even
        if the field only accepts up to 20.  Defaults to full normalization."""
        return self.normalize(raw, ctx)

    def serialize(self, value: Any) -> Any:
        """JSON-safe form of a normalized value (what the API returns and sync transmits)."""
        return value

    # -- metadata -----------------------------------------------------------------------------
    def describe(self, *, detail: bool = False) -> dict[str, Any]:
        info: dict[str, Any] = {
            "code": self.code, "label": self.label, "description": self.description, "category": self.category,
            "icon": self.icon, "capabilities": self.capabilities.as_dict(),
        }
        if detail:
            info["config_schema"] = [f.describe() for f in self.config_fields]
            info["default_config"] = self.defaults()
            info["example_value"] = self.example
        return info

    def invalid(self, code: str, message: str, path: str = "") -> AnswerInvalid:
        return AnswerInvalid(code, message, path)
