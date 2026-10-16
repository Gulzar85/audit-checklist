"""The fifteen answer types.  Each is a small class: metadata, configuration schema, validation, serialization.

Normalized value contract (what the API, reports, rules, scoring and sync all see):

=================  ====================================  =========================================
type               normalized value                       serialized (JSON)
=================  ====================================  =========================================
boolean            ``bool``                               ``true`` / ``false``
yes_no             ``"yes"`` / ``"no"``                   same string
yes_no_na          ``"yes"`` / ``"no"`` / ``"na"``        same string
single_choice      the option ``value`` (``str``)         same string
multiple_choice    ``list[str]`` of option values, in     same list
                   option order, no duplicates
score              ``int``                                number
numeric            ``Decimal`` quantized to ``decimals``  decimal **string** (``"4.5"``)
text / long_text   stripped ``str``                       same string
date               ``datetime.date``                      ``"2026-10-08"``
datetime           aware ``datetime`` in UTC              ISO 8601 (``"2026-10-08T09:30:00+00:00"``)
photo / file       ``list`` of file references            same (``file_id``, ``name``, ``size``, ``content_type``)
signature          ``{"signer_name", "file_id"}``         same
matrix             ``{row_key: column_key}`` (single) or  same
                   ``{row_key: [column_keys]}`` (multiple)
=================  ====================================  =========================================
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .base import AnswerContext, AnswerTypeDefinition
from .capabilities import Capabilities
from .exceptions import AnswerInvalid, ConfigurationError
from .files import ALLOWED_EXTENSIONS, content_type_for
from .schemas import ConfigField, parse_iso_date, parse_iso_datetime, to_decimal

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _text(raw: Any, what: str = "value") -> str:
    if not isinstance(raw, str):
        raise AnswerInvalid("type", f"The {what} must be text.")
    return raw


# ---------------------------------------------------------------------------
# Choice family
# ---------------------------------------------------------------------------
class BooleanType(AnswerTypeDefinition):
    code, label, category, icon, preview = "boolean", "Boolean", "choice", "toggle-left", "boolean"
    comparison_family = "equality"
    description = "A true / false answer."
    capabilities = Capabilities(produces_scalar=True)
    example = True

    def normalize(self, raw: Any, ctx: AnswerContext) -> bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false", "1", "0"):
            return raw.strip().lower() in ("true", "1")
        raise AnswerInvalid("invalid", "Answer must be true or false.")

    def is_empty(self, raw: Any) -> bool:
        return raw is None or (isinstance(raw, str) and not raw.strip())  # False is an answer


class _FixedChoice(AnswerTypeDefinition):
    category, icon = "choice", "circle-dot"
    comparison_family = "equality"
    allowed: tuple[str, ...] = ()
    aliases: dict[str, str] = {}
    capabilities = Capabilities(supports_options=True, has_fixed_options=True)

    def normalize(self, raw: Any, ctx: AnswerContext) -> str:
        value = _text(raw).strip().lower()
        value = self.aliases.get(value, value)
        if value not in self.allowed:
            raise AnswerInvalid("invalid_choice", "Choose one of: " + ", ".join(self.allowed) + ".")
        return value


class YesNoType(_FixedChoice):
    code, label, preview = "yes_no", "Yes / No", "yes_no"
    description = "A two-way answer."
    allowed = ("yes", "no")
    example = "yes"


class YesNoNaType(_FixedChoice):
    code, label, preview = "yes_no_na", "Yes / No / N/A", "yes_no_na"
    description = "Yes or No, or Not applicable (a real answer, so a required item can still be N/A)."
    allowed = ("yes", "no", "na")
    aliases = {"n/a": "na"}
    example = "na"


def _resolve_option(raw: Any, ctx: AnswerContext) -> str:
    """Map a submitted option reference to one of *this item's* option values (value or id), or reject."""
    if not isinstance(raw, str) or not raw.strip():
        raise AnswerInvalid("type", "An option must be given as text.")
    ref = raw.strip()
    for option in ctx.options:  # only the item's own options are ever searched
        if ref == option.value or ref == option.id:
            return option.value
    raise AnswerInvalid("invalid_option", "That option is not available for this question.")


class SingleChoiceType(AnswerTypeDefinition):
    code, label, category, icon, preview = "single_choice", "Single choice", "choice", "list", "single"
    comparison_family = "choice"
    description = "Pick exactly one of the defined options."
    capabilities = Capabilities(requires_options=True, supports_options=True)
    config_fields = (
        ConfigField("display", "Display as", "choice", default="radio", choices=(("radio", "Radio buttons"), ("dropdown", "Dropdown"))),
    )
    example = "good"

    def normalize(self, raw: Any, ctx: AnswerContext) -> str:
        if isinstance(raw, (list, tuple, dict)):
            raise AnswerInvalid("type", "Choose exactly one option.")
        return _resolve_option(raw, ctx)


class MultipleChoiceType(AnswerTypeDefinition):
    code, label, category, icon, preview = "multiple_choice", "Multiple choice", "choice", "list-checks", "multiple"
    comparison_family = "multi_choice"
    rule_constraints = ("min_select", "max_select")
    description = "Pick any number of the defined options."
    capabilities = Capabilities(requires_options=True, supports_options=True, produces_scalar=False)
    config_fields = (
        ConfigField("min_select", "Minimum selections", "int", default=0, min=0, max=100),
        ConfigField("max_select", "Maximum selections", "int", min=1, max=100, help="Leave blank for no limit."),
    )
    example = ["cleaning", "safety"]

    def check_config(self, cleaned: dict[str, Any]) -> None:
        if "max_select" in cleaned and cleaned["max_select"] < cleaned.get("min_select", 0):
            raise ConfigurationError("The maximum selections cannot be below the minimum.", "max_select")

    def normalize(self, raw: Any, ctx: AnswerContext) -> list[str]:
        if not isinstance(raw, (list, tuple)):
            raise AnswerInvalid("type", "Provide a list of options.")
        chosen = {_resolve_option(entry, ctx) for entry in raw}  # duplicates collapse here
        order = {o.value: i for i, o in enumerate(sorted(ctx.options, key=lambda o: (o.sort_order, o.value)))}
        value = sorted(chosen, key=order.__getitem__)
        low, high = ctx.config.get("min_select", 0), ctx.config.get("max_select")
        if len(value) < low:
            raise AnswerInvalid("too_few", f"Select at least {low}.")
        if high is not None and len(value) > high:
            raise AnswerInvalid("too_many", f"Select at most {high}.")
        return value


# ---------------------------------------------------------------------------
# Value family
# ---------------------------------------------------------------------------
class ScoreType(AnswerTypeDefinition):
    """A bounded whole-number rating.  An *answer* score; the checklist score is a later, separate capability."""

    code, label, category, icon, preview = "score", "Score", "value", "star", "score"
    comparison_family = "ordered"
    rule_constraints = ("min", "max")
    description = "A rating within a range (for example 1-5 or 0-10)."
    capabilities = Capabilities(supports_score=True, supports_numeric_validation=True)
    config_fields = (
        ConfigField("min", "Minimum", "int", default=1, min=0, max=100),
        ConfigField("max", "Maximum", "int", default=5, min=1, max=100),
        ConfigField("step", "Step", "int", default=1, min=1, max=100),
        ConfigField("default", "Default value", "int", min=0, max=100, help="Optional pre-selected score."),
        ConfigField("display", "Display as", "choice", default="numeric",
                    choices=(("numeric", "Numbers"), ("stars", "Stars"), ("slider", "Slider"))),
    )
    example = 4

    def check_config(self, c: dict[str, Any]) -> None:
        low, high, step = c["min"], c["max"], c["step"]
        if low >= high:
            raise ConfigurationError("The minimum score must be below the maximum.", "min")
        if (high - low) % step:
            raise ConfigurationError("The step must divide the range exactly (so the maximum can be chosen).", "step")
        if "default" in c and not (low <= c["default"] <= high and (c["default"] - low) % step == 0):
            raise ConfigurationError("The default must be a valid score.", "default")

    def parse_operand(self, raw: Any, ctx: AnswerContext) -> int:
        if isinstance(raw, bool):
            raise AnswerInvalid("type", "Score must be a whole number.")
        try:
            number = to_decimal(raw)
        except ConfigurationError:
            raise AnswerInvalid("type", "Score must be a whole number.") from None
        if number != number.to_integral_value():
            raise AnswerInvalid("type", "Score must be a whole number.")
        return int(number)

    def normalize(self, raw: Any, ctx: AnswerContext) -> int:
        value, low, high, step = self.parse_operand(raw, ctx), ctx.config["min"], ctx.config["max"], ctx.config["step"]
        if not low <= value <= high:
            raise AnswerInvalid("out_of_range", f"Score must be between {low} and {high}.")
        if (value - low) % step:
            raise AnswerInvalid("invalid_step", f"Score must be {low}, {low + step}, {low + 2 * step} … in steps of {step}.")
        return value


class NumericType(AnswerTypeDefinition):
    code, label, category, icon, preview = "numeric", "Numeric", "value", "hash", "numeric"
    comparison_family = "ordered"
    rule_constraints = ("min", "max")
    description = "A number or measurement, with optional range, precision and unit (°C, kg, %, minutes ...)."
    capabilities = Capabilities(supports_numeric_validation=True)
    config_fields = (
        ConfigField("min", "Minimum", "number", min=-1e12, max=1e12),
        ConfigField("max", "Maximum", "number", min=-1e12, max=1e12),
        ConfigField("decimals", "Decimal places", "int", default=2, min=0, max=6, help="0 accepts whole numbers only."),
        ConfigField("unit", "Unit", "text", max_length=20, help="Shown beside the value, e.g. °C, kg, %."),
    )
    example = "4.5"

    def check_config(self, c: dict[str, Any]) -> None:
        if "min" in c and "max" in c and c["min"] > c["max"]:
            raise ConfigurationError("The minimum cannot exceed the maximum.", "min")

    def parse_operand(self, raw: Any, ctx: AnswerContext) -> Decimal:
        if isinstance(raw, bool):
            raise AnswerInvalid("type", "Enter a number.")
        try:
            number = to_decimal(raw)
        except ConfigurationError:
            raise AnswerInvalid("type", "Enter a number.") from None
        if abs(number) >= Decimal("1e12"):
            raise AnswerInvalid("out_of_range", "That number is too large.")
        decimals = ctx.config.get("decimals", 2)
        exponent = number.as_tuple().exponent
        if isinstance(exponent, int) and -exponent > decimals and number != number.quantize(Decimal(1).scaleb(-decimals)):
            raise AnswerInvalid(
                "precision", "Enter a whole number." if decimals == 0 else f"Use at most {decimals} decimal place{'s' if decimals != 1 else ''}."
            )
        return number.quantize(Decimal(1).scaleb(-decimals))

    def normalize(self, raw: Any, ctx: AnswerContext) -> Decimal:
        value = self.parse_operand(raw, ctx)
        unit = f" {ctx.config['unit']}" if ctx.config.get("unit") else ""
        low, high = ctx.config.get("min"), ctx.config.get("max")
        low_d = Decimal(str(low)) if low is not None else None
        high_d = Decimal(str(high)) if high is not None else None
        if low_d is not None and value < low_d:
            raise AnswerInvalid("out_of_range", f"Value must be at least {low}{unit}.")
        if high_d is not None and value > high_d:
            raise AnswerInvalid("out_of_range", f"Value must be at most {high}{unit}.")
        return value

    def serialize(self, value: Decimal) -> str:
        return format(value, "f")


class _TextBase(AnswerTypeDefinition):
    category, icon = "value", "text"
    comparison_family = "text"
    rule_constraints = ("min_length", "max_length")
    multiline = False
    hard_max = 1000
    default_max = 255
    capabilities = Capabilities()

    @property
    def config_fields(self) -> tuple[ConfigField, ...]:  # type: ignore[override]
        return (
            ConfigField("min_length", "Minimum length", "int", default=0, min=0, max=self.hard_max),
            ConfigField("max_length", "Maximum length", "int", default=self.default_max, min=1, max=self.hard_max),
        )

    def check_config(self, c: dict[str, Any]) -> None:
        if c["min_length"] > c["max_length"]:
            raise ConfigurationError("The minimum length cannot exceed the maximum.", "min_length")

    def parse_operand(self, raw: Any, ctx: AnswerContext) -> str:
        value = _text(raw, "answer")
        if self.multiline:
            value = value.replace("\r\n", "\n").replace("\r", "\n")
        elif "\n" in value or "\r" in value:
            raise AnswerInvalid("multiline", "This answer must be on a single line.")
        if _CONTROL.search(value):
            raise AnswerInvalid("characters", "The answer contains characters that are not allowed.")
        return value.strip()

    def normalize(self, raw: Any, ctx: AnswerContext) -> str:
        value = self.parse_operand(raw, ctx)
        low, high = ctx.config.get("min_length", 0), ctx.config.get("max_length", self.default_max)
        if len(value) < low:
            raise AnswerInvalid("too_short", f"Enter at least {low} characters.")
        if len(value) > high:
            raise AnswerInvalid("too_long", f"Enter at most {high} characters.")
        return value


class TextType(_TextBase):
    code, label, preview = "text", "Short text", "text"
    description = "A single line of text."
    example = "Everything is operational."


class LongTextType(_TextBase):
    code, label, preview, icon = "long_text", "Long text", "long_text", "align-left"
    description = "Several lines of text (comments, observations)."
    multiline, hard_max, default_max = True, 10000, 5000
    example = "Observed grease build-up on the extraction hood.\nRaised with the manager."


# ---------------------------------------------------------------------------
# Date & time
# ---------------------------------------------------------------------------
class DateType(AnswerTypeDefinition):
    code, label, category, icon, preview = "date", "Date", "datetime", "calendar", "date"
    comparison_family = "ordered"
    rule_constraints = ("min_date", "max_date")
    description = "A calendar date."
    capabilities = Capabilities()
    config_fields = (
        ConfigField("min_date", "Earliest date", "date"),
        ConfigField("max_date", "Latest date", "date"),
    )
    example = "2026-10-08"

    def check_config(self, c: dict[str, Any]) -> None:
        if "min_date" in c and "max_date" in c and c["min_date"] > c["max_date"]:
            raise ConfigurationError("The earliest date cannot be after the latest.", "min_date")

    def parse_operand(self, raw: Any, ctx: AnswerContext) -> date:
        if isinstance(raw, datetime) or not isinstance(raw, (str, date)):
            raise AnswerInvalid("type", "Enter a date (YYYY-MM-DD).")
        try:
            return parse_iso_date(raw, "")
        except ConfigurationError:
            raise AnswerInvalid("type", "Enter a valid date (YYYY-MM-DD).") from None

    def normalize(self, raw: Any, ctx: AnswerContext) -> date:
        value = self.parse_operand(raw, ctx)
        low, high = ctx.config.get("min_date"), ctx.config.get("max_date")
        if low and value < date.fromisoformat(low):
            raise AnswerInvalid("out_of_range", f"The date cannot be before {low}.")
        if high and value > date.fromisoformat(high):
            raise AnswerInvalid("out_of_range", f"The date cannot be after {high}.")
        return value

    def serialize(self, value: date) -> str:
        return value.isoformat()


class DateTimeType(AnswerTypeDefinition):
    code, label, category, icon, preview = "datetime", "Date and time", "datetime", "calendar-clock", "datetime"
    comparison_family = "ordered"
    rule_constraints = ("min_datetime", "max_datetime")
    description = "A date and time. A time without an offset is read in the project's time zone; stored in UTC."
    capabilities = Capabilities()
    config_fields = (
        ConfigField("min_datetime", "Earliest", "datetime"),
        ConfigField("max_datetime", "Latest", "datetime"),
    )
    example = "2026-10-08T09:30:00+00:00"

    def check_config(self, c: dict[str, Any]) -> None:
        if "min_datetime" in c and "max_datetime" in c and c["min_datetime"] > c["max_datetime"]:
            raise ConfigurationError("The earliest time cannot be after the latest.", "min_datetime")

    def parse_operand(self, raw: Any, ctx: AnswerContext) -> datetime:
        if not isinstance(raw, (str, datetime)):
            raise AnswerInvalid("type", "Enter a date and time.")
        try:
            return parse_iso_datetime(raw, "")
        except (ConfigurationError, OverflowError):
            raise AnswerInvalid("type", "Enter a valid date and time (ISO 8601).") from None

    def normalize(self, raw: Any, ctx: AnswerContext) -> datetime:
        value = self.parse_operand(raw, ctx)
        low, high = ctx.config.get("min_datetime"), ctx.config.get("max_datetime")
        if low and value < datetime.fromisoformat(low):
            raise AnswerInvalid("out_of_range", "The time is earlier than allowed.")
        if high and value > datetime.fromisoformat(high):
            raise AnswerInvalid("out_of_range", "The time is later than allowed.")
        return value

    def serialize(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------
# Media (contracts only: storage and scanning belong to the evidence phase)
# ---------------------------------------------------------------------------
class _FileBase(AnswerTypeDefinition):
    category = "media"
    rule_constraints = ("max_files",)
    kind = "file"
    default_extensions: tuple[str, ...] = ()
    capabilities = Capabilities(is_media=True, produces_scalar=False, supports_comment=True)
    example = [{"file_id": "0b6c7f0e-3d4a-4a46-9b8e-2f1e5d1c7a10", "name": "storage.jpg", "size": 182044, "content_type": "image/jpeg"}]

    @property
    def config_fields(self) -> tuple[ConfigField, ...]:  # type: ignore[override]
        universe = ALLOWED_EXTENSIONS[self.kind]
        return (
            ConfigField("max_files", "Maximum files", "int", default=1, min=1, max=10),
            ConfigField("max_size_mb", "Maximum size per file (MB)", "int", default=10, min=1, max=50),
            ConfigField("allowed_extensions", "Allowed file types", "list", default=list(self.default_extensions),
                        universe=universe, max_items=len(universe), max_length=10,
                        help="Allowed: " + ", ".join(universe)),
        )

    def check_config(self, c: dict[str, Any]) -> None:
        if not c.get("allowed_extensions"):
            raise ConfigurationError("Allow at least one file type.", "allowed_extensions")

    def normalize(self, raw: Any, ctx: AnswerContext) -> list[dict[str, Any]]:
        entries = [raw] if isinstance(raw, dict) else raw
        if not isinstance(entries, (list, tuple)) or not all(isinstance(e, dict) for e in entries):
            raise AnswerInvalid("type", "Provide a list of uploaded files.")
        config = ctx.config
        max_files = config.get("max_files", 1)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            path = f"[{index}]"
            unknown = set(entry) - {"file_id", "name", "size", "content_type"}
            if unknown:
                raise AnswerInvalid("unknown_field", "Unexpected file fields: " + ", ".join(sorted(unknown)) + ".", path)
            try:
                file_id = str(uuid.UUID(str(entry.get("file_id"))))
            except (ValueError, AttributeError):
                raise AnswerInvalid("file_id", "Each file needs the id returned by the upload.", path) from None
            name = entry.get("name")
            if (not isinstance(name, str) or not 1 <= len(name) <= 255 or _CONTROL.search(name)
                    or "/" in name or "\\" in name or ".." in name):
                raise AnswerInvalid("name", "The file name is not valid.", path)
            extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if extension not in config.get("allowed_extensions", []):
                raise AnswerInvalid("extension", "This file type is not allowed here.", path)
            size = entry.get("size")
            if isinstance(size, bool) or not isinstance(size, int) or size < 1:
                raise AnswerInvalid("size", "The file size is not valid.", path)
            if size > config.get("max_size_mb", 10) * 1024 * 1024:
                raise AnswerInvalid("too_large", f"Files may be at most {config.get('max_size_mb', 10)} MB.", path)
            server_type = content_type_for(extension)
            declared = entry.get("content_type")
            # The client's claim is only ever a consistency check; the stored type comes from the extension map
            # (and, once bytes are available, from content sniffing - see ``files.verify_content``).
            if declared not in (None, "") and (not isinstance(declared, str) or declared.lower() != server_type):
                raise AnswerInvalid("content_type", "The declared file type does not match the file extension.", path)
            if file_id in seen:
                continue
            seen.add(file_id)
            out.append({"file_id": file_id, "name": name, "size": size, "content_type": server_type})
        if len(out) > max_files:
            raise AnswerInvalid("too_many", f"At most {max_files} file{'s' if max_files != 1 else ''} may be attached.")
        return out


class PhotoType(_FileBase):
    code, label, icon, preview = "photo", "Photo", "camera", "photo"
    description = "One or more photos (references to uploads; storage arrives with the evidence phase)."
    kind = "photo"
    default_extensions = ("jpg", "jpeg", "png", "webp", "heic")

    @property
    def config_fields(self) -> tuple[ConfigField, ...]:  # type: ignore[override]
        return (*super().config_fields,
                ConfigField("source", "Photo source", "choice", default="any",
                            choices=(("any", "Camera or library"), ("camera", "Camera only"))))


class FileType(_FileBase):
    code, label, icon, preview = "file", "File", "paperclip", "file"
    description = "One or more documents (references to uploads; storage arrives with the evidence phase)."
    kind = "file"
    default_extensions = ("pdf", "doc", "docx", "xls", "xlsx", "csv", "txt", "jpg", "jpeg", "png")


class SignatureType(AnswerTypeDefinition):
    code, label, category, icon, preview = "signature", "Signature", "media", "pen-line", "signature"
    description = "A signature: the signer's name and, optionally, a captured signature image (stored later)."
    capabilities = Capabilities(is_media=True, produces_scalar=False, supports_comment=False)
    config_fields = (
        ConfigField("signer_label", "Who signs", "text", default="Signer", max_length=80, help="e.g. Auditor, Auditee, Manager."),
        ConfigField("require_name", "Require the signer's name", "bool", default=True),
    )
    example = {"signer_name": "A. Khan", "file_id": None}

    def normalize(self, raw: Any, ctx: AnswerContext) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AnswerInvalid("type", "Provide the signer and signature.")
        unknown = set(raw) - {"signer_name", "file_id"}
        if unknown:
            raise AnswerInvalid("unknown_field", "Unexpected signature fields: " + ", ".join(sorted(unknown)) + ".")
        name = raw.get("signer_name")
        if name not in (None, "") and (not isinstance(name, str) or len(name.strip()) > 120 or _CONTROL.search(name)):
            raise AnswerInvalid("signer_name", "The signer's name is not valid.")
        name = (name or "").strip()
        file_id = raw.get("file_id")
        if file_id not in (None, ""):
            try:
                file_id = str(uuid.UUID(str(file_id)))
            except ValueError:
                raise AnswerInvalid("file_id", "The signature image reference is not valid.") from None
        else:
            file_id = None
        if ctx.config.get("require_name", True) and not name:
            raise AnswerInvalid("signer_name", "Enter the signer's name.")
        if not name and not file_id:
            raise AnswerInvalid("signature", "A signature needs a name or a signature image.")
        return {"signer_name": name, "file_id": file_id}


# ---------------------------------------------------------------------------
# Advanced
# ---------------------------------------------------------------------------
class MatrixType(AnswerTypeDefinition):
    """Rows x columns grid.  Single mode: one column per row (radio); multiple mode: any columns per row."""

    code, label, category, icon, preview = "matrix", "Matrix (grid)", "advanced", "grid-3x3", "matrix"
    description = "A grid: for each row choose a column (for example each area: Clean / Damaged / Missing)."
    capabilities = Capabilities(produces_scalar=False)
    config_fields = (
        ConfigField("rows", "Rows", "labels", max_items=50, widget="textarea", help="One per line."),
        ConfigField("columns", "Columns", "labels", max_items=20, widget="textarea", help="One per line."),
        ConfigField("mode", "Cell mode", "choice", default="single",
                    choices=(("single", "One column per row"), ("multiple", "Any columns per row"))),
        ConfigField("require_all_rows", "Every row must be answered", "bool", default=False),
    )
    example = {"dining_area": "clean", "kitchen": "damaged"}

    def check_config(self, c: dict[str, Any]) -> None:
        for key in ("rows", "columns"):
            if not c.get(key):
                raise ConfigurationError(f"A matrix needs at least one {key[:-1]}.", key)

    def normalize(self, raw: Any, ctx: AnswerContext) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AnswerInvalid("type", "Provide an answer for each row.")
        rows = {r["key"] for r in ctx.config["rows"]}
        columns = {c["key"] for c in ctx.config["columns"]}
        multiple = ctx.config.get("mode", "single") == "multiple"
        out: dict[str, Any] = {}
        for row, cell in raw.items():
            if row not in rows:
                raise AnswerInvalid("invalid_row", "That row does not exist in this matrix.", str(row)[:64])
            if cell in (None, "", []):
                continue
            if multiple:
                if not isinstance(cell, (list, tuple)):
                    raise AnswerInvalid("type", "Provide a list of columns for this row.", row)
                picked = []
                for column in cell:
                    if column not in columns:
                        raise AnswerInvalid("invalid_column", "That column does not exist in this matrix.", row)
                    if column not in picked:
                        picked.append(column)
                out[row] = [c["key"] for c in ctx.config["columns"] if c["key"] in picked]
            else:
                if isinstance(cell, (list, tuple, dict)) or cell not in columns:
                    raise AnswerInvalid("invalid_column", "Choose one valid column for this row.", row)
                out[row] = cell
        if ctx.config.get("require_all_rows"):
            missing = [r["label"] for r in ctx.config["rows"] if r["key"] not in out]
            if missing:
                raise AnswerInvalid("row_required", "Answer every row. Missing: " + ", ".join(missing) + ".")
        # Rows come back in definition order so equal answers serialize identically.
        return {r["key"]: out[r["key"]] for r in ctx.config["rows"] if r["key"] in out}

    def is_empty(self, raw: Any) -> bool:
        if isinstance(raw, dict):
            return all(v in (None, "", []) for v in raw.values())
        return super().is_empty(raw)


ALL_DEFINITIONS: tuple[type[AnswerTypeDefinition], ...] = (
    BooleanType, YesNoType, YesNoNaType, SingleChoiceType, MultipleChoiceType, ScoreType, NumericType, TextType,
    LongTextType, DateType, DateTimeType, PhotoType, FileType, SignatureType, MatrixType,
)
