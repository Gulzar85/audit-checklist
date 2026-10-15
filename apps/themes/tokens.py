"""The design-token registry: the *only* vocabulary a theme can speak.

A theme stores **tokens** - validated values for a closed list of semantic names - never
CSS.  This module is the allow-list.  Every token has a kind that decides what is accepted
and how it is turned into CSS:

* ``color``  - ``#rgb``/``#rrggbb``, ``rgb()``/``hsl()`` (opaque).  Normalised to ``#rrggbb``.
* ``px``     - a number in a range, emitted as ``rem``.
* ``number`` - a unitless number in a range.
* ``choice`` - one key from a fixed mapping; the CSS comes from *our* table, not the user.

Nothing the user types ever reaches a stylesheet verbatim: colours are re-serialised from
parsed integers, numbers are re-formatted from parsed floats, choices are looked up.  That
is what makes injection impossible by construction (ADR 0026, ``docs/themes/security.md``).

Storage shape (``Theme.tokens``)::

    {"light": {"primary": "#4f46e5", ...},   # mode-specific colours
     "dark":  {"primary": "#818cf8", ...},
     "shared": {"font_family": "sans", "radius_md": 8, ...}}

A theme holds *overrides*; anything it leaves out falls through to the layer below
(application theme, then the built-in defaults - see ``selectors.resolve_theme``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ValidationError

from . import contrast as colour

MODES = ("light", "dark")
SHARED = "shared"
SECTIONS = (*MODES, SHARED)

# --- groups (how the editor sections the tokens) ------------------------------------
BRAND = "brand"
STATUS = "status"
SURFACE = "surface"
NAVIGATION = "navigation"
TYPOGRAPHY = "typography"
SHAPE = "shape"
COMPONENTS = "components"
DENSITY = "density"
SHADOWS = "shadows"

STATUS_NAMES = ("success", "warning", "danger", "info", "neutral")

# --- fixed CSS tables (the only CSS a ``choice`` token can ever produce) -------------
FONT_STACKS: dict[str, tuple[str, str]] = {
    "sans": (
        "Sans-serif (Inter)",
        'Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
    ),
    "system": (
        "System interface font",
        'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    ),
    "humanist": (
        "Humanist",
        '"Segoe UI", Candara, "Trebuchet MS", Optima, Calibri, "Gill Sans", sans-serif',
    ),
    "serif": ("Serif", 'ui-serif, Georgia, Cambria, "Times New Roman", Times, serif'),
    "mono": (
        "Monospace",
        'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
    ),
}

RADIUS_REFERENCES: dict[str, tuple[str, str]] = {
    "none": ("Square", "0"),
    "sm": ("Small", "var(--radius-sm)"),
    "md": ("Medium", "var(--radius-md)"),
    "lg": ("Large", "var(--radius-lg)"),
    "full": ("Pill", "var(--radius-full)"),
}

SHADOW_LEVELS: dict[str, tuple[str, str]] = {
    "none": ("None", "none"),
    "subtle": ("Subtle", "0 1px 2px 0 rgb(0 0 0 / 0.05)"),
    "medium": ("Medium", "0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1)"),
    "strong": ("Strong", "0 10px 15px -3px rgb(0 0 0 / 0.15), 0 4px 6px -4px rgb(0 0 0 / 0.15)"),
}

DENSITY_SCALES: dict[str, tuple[str, str]] = {
    "compact": ("Compact", "0.875"),
    "comfortable": ("Comfortable", "1"),
    "spacious": ("Spacious", "1.125"),
}

CONTROL_PADDING: dict[str, tuple[str, str]] = {
    "compact": ("Compact", "0.4375rem"),
    "normal": ("Normal", "0.625rem"),
    "relaxed": ("Relaxed", "0.8125rem"),
}

HEADING_WEIGHTS: dict[str, tuple[str, str]] = {
    "500": ("Medium", "500"),
    "600": ("Semibold", "600"),
    "700": ("Bold", "700"),
    "800": ("Extra bold", "800"),
}

BODY_WEIGHTS: dict[str, tuple[str, str]] = {
    "300": ("Light", "300"),
    "400": ("Regular", "400"),
    "500": ("Medium", "500"),
}


@dataclass(frozen=True)
class Token:
    key: str
    group: str
    kind: str  # color | px | number | choice
    label: str
    css: str  # the CSS custom property this token drives
    light: Any
    dark: Any
    help: str = ""
    mode_specific: bool = False
    options: dict[str, tuple[str, str]] = field(default_factory=dict)  # choice: key -> (label, css)
    minimum: float = 0
    maximum: float = 0
    step: float = 1

    @property
    def default(self) -> Any:
        return self.light

    def choices(self) -> list[tuple[str, str]]:
        return [(key, label) for key, (label, _css) in self.options.items()]


def _color(key: str, group: str, label: str, light: str, dark: str, help: str = "") -> Token:  # noqa: A002
    return Token(
        key=key,
        group=group,
        kind="color",
        label=label,
        css="--color-" + key.replace("_", "-"),
        light=light,
        dark=dark,
        help=help,
        mode_specific=True,
    )


def _shared(key: str, group: str, kind: str, label: str, css: str, default: Any, help: str = "", **extra: Any) -> Token:  # noqa: A002
    return Token(key=key, group=group, kind=kind, label=label, css=css, light=default, dark=default, help=help, **extra)


_TOKEN_LIST: tuple[Token, ...] = (
    # --- Brand colours ---------------------------------------------------------------
    _color("primary", BRAND, "Primary", "#4f46e5", "#818cf8", "Main actions, key highlights."),
    _color("primary_hover", BRAND, "Primary (hover)", "#4338ca", "#a5b4fc"),
    _color("primary_active", BRAND, "Primary (pressed)", "#3730a3", "#c7d2fe"),
    _color("primary_foreground", BRAND, "Text on primary", "#ffffff", "#0f172a", "Must stay readable on the primary colour."),
    _color("secondary", BRAND, "Secondary", "#ffffff", "#1e293b", "Secondary buttons."),
    _color("secondary_foreground", BRAND, "Text on secondary", "#334155", "#e2e8f0"),
    _color("accent", BRAND, "Accent", "#0e7490", "#22d3ee", "Decorative emphasis."),
    _color("link", BRAND, "Links", "#4338ca", "#a5b4fc"),
    _color("focus", BRAND, "Focus ring", "#6366f1", "#a5b4fc", "Keyboard focus indicator."),
    # --- Surfaces --------------------------------------------------------------------
    _color("background", SURFACE, "Page background", "#f1f5f9", "#020617"),
    _color("surface", SURFACE, "Cards and panels", "#ffffff", "#0f172a"),
    _color("surface_elevated", SURFACE, "Raised surfaces", "#ffffff", "#1e293b", "Menus, dialogs."),
    _color("foreground", SURFACE, "Text", "#1e293b", "#f1f5f9"),
    _color("muted", SURFACE, "Muted background", "#f8fafc", "#1e293b", "Table headers, hovered rows."),
    _color("muted_foreground", SURFACE, "Muted text", "#64748b", "#94a3b8", "Help text, captions."),
    _color("border", SURFACE, "Borders", "#cbd5e1", "#334155"),
    # --- Status ----------------------------------------------------------------------
    _color("success", STATUS, "Success", "#047857", "#34d399"),
    _color("warning", STATUS, "Warning", "#b45309", "#fbbf24"),
    _color("danger", STATUS, "Danger", "#dc2626", "#f87171"),
    _color("info", STATUS, "Info", "#0369a1", "#38bdf8"),
    _color("neutral", STATUS, "Neutral", "#475569", "#94a3b8"),
    # --- Navigation ------------------------------------------------------------------
    _color("sidebar_background", NAVIGATION, "Navigation background", "#ffffff", "#0f172a"),
    _color("sidebar_foreground", NAVIGATION, "Navigation text", "#475569", "#cbd5e1"),
    _color("sidebar_active", NAVIGATION, "Active item", "#4338ca", "#a5b4fc"),
    _color("sidebar_hover", NAVIGATION, "Hovered item background", "#f1f5f9", "#1e293b"),
    _color("sidebar_border", NAVIGATION, "Navigation border", "#e2e8f0", "#1e293b"),
    # --- Typography ------------------------------------------------------------------
    _shared("font_family", TYPOGRAPHY, "choice", "Font", "--font-family", "sans",
            "Safe font stacks only - no remote fonts.", options=FONT_STACKS),
    _shared("font_size_base", TYPOGRAPHY, "px", "Base text size", "--font-size-base", 16,
            minimum=12, maximum=20, step=1),
    _shared("font_size_small", TYPOGRAPHY, "px", "Small text size", "--font-size-small", 14,
            minimum=10, maximum=16, step=1),
    _shared("font_size_large", TYPOGRAPHY, "px", "Large text size", "--font-size-large", 18,
            minimum=14, maximum=28, step=1),
    _shared("heading_weight", TYPOGRAPHY, "choice", "Heading weight", "--heading-weight", "600", options=HEADING_WEIGHTS),
    _shared("body_weight", TYPOGRAPHY, "choice", "Body weight", "--body-weight", "400", options=BODY_WEIGHTS),
    _shared("line_height", TYPOGRAPHY, "number", "Line height", "--line-height", 1.5,
            minimum=1.2, maximum=2.0, step=0.05),
    # --- Shape -----------------------------------------------------------------------
    _shared("radius_sm", SHAPE, "px", "Small radius", "--radius-sm", 6, minimum=0, maximum=16, step=1),
    _shared("radius_md", SHAPE, "px", "Medium radius", "--radius-md", 8, minimum=0, maximum=24, step=1),
    _shared("radius_lg", SHAPE, "px", "Large radius", "--radius-lg", 12, minimum=0, maximum=32, step=1),
    # --- Component shape -------------------------------------------------------------
    _shared("button_radius", COMPONENTS, "choice", "Buttons", "--button-radius", "md", options=RADIUS_REFERENCES),
    _shared("input_radius", COMPONENTS, "choice", "Form fields", "--input-radius", "md", options=RADIUS_REFERENCES),
    _shared("card_radius", COMPONENTS, "choice", "Cards", "--card-radius", "lg", options=RADIUS_REFERENCES),
    _shared("table_radius", COMPONENTS, "choice", "Tables", "--table-radius", "lg", options=RADIUS_REFERENCES),
    _shared("modal_radius", COMPONENTS, "choice", "Dialogs", "--modal-radius", "lg", options=RADIUS_REFERENCES),
    # --- Density ---------------------------------------------------------------------
    _shared("density", DENSITY, "choice", "Spacing density", "--spacing-scale", "comfortable",
            "Scales the whole spacing system.", options=DENSITY_SCALES),
    _shared("component_padding", DENSITY, "choice", "Control padding", "--control-padding-y", "normal",
            "Vertical padding of buttons and fields.", options=CONTROL_PADDING),
    # --- Shadows ---------------------------------------------------------------------
    _shared("shadow_sm", SHADOWS, "choice", "Small shadow", "--shadow-sm", "subtle", options=SHADOW_LEVELS),
    _shared("shadow_md", SHADOWS, "choice", "Medium shadow", "--shadow-md", "medium", options=SHADOW_LEVELS),
    _shared("shadow_lg", SHADOWS, "choice", "Large shadow", "--shadow-lg", "strong", options=SHADOW_LEVELS),
)

TOKENS: dict[str, Token] = {token.key: token for token in _TOKEN_LIST}
MODE_TOKENS: tuple[Token, ...] = tuple(t for t in _TOKEN_LIST if t.mode_specific)
SHARED_TOKENS: tuple[Token, ...] = tuple(t for t in _TOKEN_LIST if not t.mode_specific)

# --- colour parsing -------------------------------------------------------------------
_HEX = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_RGB = re.compile(
    r"^rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*(?:[,/]\s*(\d*\.?\d+%?)\s*)?\)$",
    re.IGNORECASE,
)
_HSL = re.compile(
    r"^hsla?\(\s*(-?\d{1,3}(?:\.\d+)?)(?:deg)?\s*[, ]\s*(\d{1,3}(?:\.\d+)?)%\s*[, ]\s*(\d{1,3}(?:\.\d+)?)%\s*(?:[,/]\s*(\d*\.?\d+%?)\s*)?\)$",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_MAX_COLOR_LENGTH = 48

APPROVED_COLOR_FORMATS = "#RGB, #RRGGBB, rgb(r, g, b) or hsl(h, s%, l%)"


def _alpha_is_opaque(raw: str | None) -> bool:
    if raw is None:
        return True
    value = float(raw[:-1]) / 100 if raw.endswith("%") else float(raw)
    return value == 1


def normalize_color(value: Any) -> str:
    """Parse an approved colour format into canonical lower-case ``#rrggbb``."""
    if not isinstance(value, str):
        raise ValidationError("A colour must be text.")
    text = value.strip()
    if not text or len(text) > _MAX_COLOR_LENGTH:
        raise ValidationError(f"Use a colour such as {APPROVED_COLOR_FORMATS}.")
    if match := _HEX.match(text):
        digits = match.group(1).lower()
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        return "#" + digits
    if match := _RGB.match(text):
        channels = [int(match.group(i)) for i in (1, 2, 3)]
        if any(c > 255 for c in channels):
            raise ValidationError("RGB channels must be between 0 and 255.")
        if not _alpha_is_opaque(match.group(4)):
            raise ValidationError("Transparent colours are not supported; use an opaque colour.")
        return colour.to_hex(tuple(channels))  # type: ignore[arg-type]
    if match := _HSL.match(text):
        hue, sat, light = (float(match.group(i)) for i in (1, 2, 3))
        if not (0 <= sat <= 100 and 0 <= light <= 100) or not (-360 <= hue <= 360):
            raise ValidationError("HSL values are out of range.")
        if not _alpha_is_opaque(match.group(4)):
            raise ValidationError("Transparent colours are not supported; use an opaque colour.")
        return _hsl_to_hex(hue % 360, sat / 100, light / 100)
    raise ValidationError(f"Use a colour such as {APPROVED_COLOR_FORMATS}.")


def _hsl_to_hex(hue: float, sat: float, light: float) -> str:
    chroma = (1 - abs(2 * light - 1)) * sat
    sector = hue / 60
    x = chroma * (1 - abs(sector % 2 - 1))
    r, g, b = [(chroma, x, 0), (x, chroma, 0), (0, chroma, x), (0, x, chroma), (x, 0, chroma), (chroma, 0, x)][
        int(sector) % 6
    ]
    m = light - chroma / 2
    return colour.to_hex(((r + m) * 255, (g + m) * 255, (b + m) * 255))


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValidationError("Enter a number.")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and _NUMBER.match(value.strip()):
        number = float(value.strip())
    else:
        raise ValidationError("Enter a number.")
    if not math.isfinite(number):
        raise ValidationError("Enter a finite number.")
    return number


def validate_token_value(key: str, value: Any) -> Any:
    """Validate one value for token ``key`` and return its canonical form."""
    token = TOKENS.get(key)
    if token is None:
        raise ValidationError(f"Unknown design token “{key}”.")
    if token.kind == "color":
        return normalize_color(value)
    if token.kind in ("px", "number"):
        number = _number(value)
        if not token.minimum <= number <= token.maximum:
            raise ValidationError(f"Must be between {token.minimum:g} and {token.maximum:g}.")
        return int(number) if token.kind == "px" and number == int(number) else round(number, 3)
    if token.kind == "choice":
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)  # numeric weights may arrive as 600 or "600"
        if not isinstance(value, str) or value not in token.options:
            allowed = ", ".join(token.options)
            raise ValidationError(f"Choose one of: {allowed}.")
        return value
    raise ValidationError("Unsupported token kind.")  # pragma: no cover - registry is closed


def validate_tokens(raw: Any) -> dict[str, dict[str, Any]]:
    """Validate a stored/imported ``tokens`` document; return its canonical form.

    Strict on shape: only ``light``/``dark``/``shared`` sections, mode-specific tokens only in
    a mode section, shared tokens only in ``shared``.  All problems are reported together.
    """
    if raw in (None, ""):
        raw = {}
    if not isinstance(raw, dict):
        raise ValidationError({"tokens": "Tokens must be an object."})
    errors: list[str] = []
    clean: dict[str, dict[str, Any]] = {}
    for section, values in raw.items():
        if section not in SECTIONS:
            errors.append(f"Unknown token section “{str(section)[:40]}”.")
            continue
        if not isinstance(values, dict):
            errors.append(f"Section “{section}” must be an object.")
            continue
        for key, value in values.items():
            token = TOKENS.get(key) if isinstance(key, str) else None
            if token is None:
                errors.append(f"{section}: unknown design token “{str(key)[:40]}”.")
                continue
            if token.mode_specific != (section != SHARED):
                where = "light/dark" if token.mode_specific else SHARED
                errors.append(f"{section}.{key}: this token belongs in “{where}”.")
                continue
            try:
                clean.setdefault(section, {})[key] = validate_token_value(key, value)
            except ValidationError as exc:
                errors.append(f"{section}.{key}: {' '.join(exc.messages)}")
    if errors:
        raise ValidationError({"tokens": errors})
    return clean


# --- layering ---------------------------------------------------------------------------
def default_tokens() -> dict[str, dict[str, Any]]:
    """The built-in complete token set (System Defaults)."""
    return {
        "light": {t.key: t.light for t in MODE_TOKENS},
        "dark": {t.key: t.dark for t in MODE_TOKENS},
        SHARED: {t.key: t.default for t in SHARED_TOKENS},
    }


def merge_tokens(*layers: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Defaults, then each layer in order (later layers win).  Layers are *override* sets."""
    merged = default_tokens()
    for layer in layers:
        for section, values in (layer or {}).items():
            if section in merged:
                merged[section].update(values)
    return merged


def diff_tokens(old: dict | None, new: dict | None) -> list[dict[str, Any]]:
    """Human-readable differences between two token override documents."""
    old, new = old or {}, new or {}
    changes: list[dict[str, Any]] = []
    for section in SECTIONS:
        before, after = old.get(section, {}), new.get(section, {})
        for key in [k for k in TOKENS if k in before or k in after]:
            if before.get(key) != after.get(key):
                token = TOKENS[key]
                changes.append(
                    {
                        "token": key,
                        "label": token.label if token.mode_specific is False else f"{token.label} ({section})",
                        "section": section,
                        "from": before.get(key),
                        "to": after.get(key),
                    }
                )
    return changes


# --- CSS emission -----------------------------------------------------------------------
def _format_number(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def emit(token: Token, value: Any) -> dict[str, str]:
    """Return the ``{custom property: css value}`` this validated token value produces."""
    if token.kind == "color":
        return {token.css: value}
    if token.kind == "px":
        return {token.css: "0" if value == 0 else f"{_format_number(value / 16)}rem"}
    if token.kind == "number":
        return {token.css: _format_number(value)}
    return {token.css: token.options[value][1]}


def mode_variables(tokens: dict[str, dict[str, Any]], mode: str) -> dict[str, str]:
    """Custom properties that differ by ``mode`` (colours + derived status foregrounds)."""
    variables: dict[str, str] = {}
    for token in MODE_TOKENS:
        variables.update(emit(token, tokens[mode][token.key]))
    for status in STATUS_NAMES:
        variables[f"--color-{status}-foreground"] = colour.best_foreground(tokens[mode][status])
    return variables


def shared_variables(tokens: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Custom properties that do not depend on the colour mode."""
    variables: dict[str, str] = {}
    for token in SHARED_TOKENS:
        variables.update(emit(token, tokens[SHARED][token.key]))
    return variables


def preview_variables(tokens: dict[str, dict[str, Any]], mode: str) -> dict[str, str]:
    """Everything needed to render a preview container in ``mode``."""
    return {**shared_variables(tokens), **mode_variables(tokens, mode)}


def pwa_colors(tokens: dict[str, dict[str, Any]], mode: str) -> dict[str, str]:
    """Manifest / ``<meta name="theme-color">`` colours, derived from approved tokens only."""
    return {
        "theme_color": tokens[mode]["sidebar_background"],
        "background_color": tokens[mode]["background"],
    }
