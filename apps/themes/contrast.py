"""Colour maths: parsing, WCAG contrast, blending and the accessibility report.

Standard followed: **WCAG 2.2 level AA** (see ``docs/themes/accessibility.md``).

* 1.4.3 Contrast (Minimum): normal text >= 4.5:1.
* 1.4.11 Non-text Contrast: focus indicators and UI component boundaries >= 3:1.

Only ``#rrggbb`` colours exist at this layer - token validation normalises every accepted
colour format to it - so the arithmetic here is exact and never has to guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    STATUS_SOFT_PERCENT,
    STATUS_TEXT_PERCENT,
    WCAG_NON_TEXT_AA,
    WCAG_TEXT_AA,
)

DARK_TEXT = "#0f172a"
LIGHT_TEXT = "#ffffff"


def to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def to_hex(rgb: tuple[float, float, float]) -> str:
    r, g, b = (max(0, min(255, round(channel))) for channel in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def relative_luminance(color: str) -> float:
    def linear(channel: int) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in to_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (relative_luminance(foreground), relative_luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def mix(first: str, second: str, first_percent: float) -> str:
    """``color-mix(in srgb, first <p>%, second)`` - the same arithmetic the browser uses."""
    weight = first_percent / 100
    a, b = to_rgb(first), to_rgb(second)
    return to_hex(tuple(weight * x + (1 - weight) * y for x, y in zip(a, b)))  # type: ignore[arg-type]


def best_foreground(background: str) -> str:
    """Text colour (white or near-black) with the higher contrast on ``background``."""
    if contrast_ratio(LIGHT_TEXT, background) >= contrast_ratio(DARK_TEXT, background):
        return LIGHT_TEXT
    return DARK_TEXT


@dataclass(frozen=True)
class ContrastCheck:
    mode: str  # "light" | "dark"
    label: str
    foreground: str
    background: str
    ratio: float
    required: float
    mandatory: bool

    @property
    def passed(self) -> bool:
        return self.ratio >= self.required

    @property
    def level(self) -> str:
        """``ok`` / ``error`` (mandatory and failing: blocks publishing) / ``warning``."""
        if self.passed:
            return "ok"
        return "error" if self.mandatory else "warning"

    @property
    def ratio_display(self) -> str:
        return f"{self.ratio:.2f}:1"


def _check(mode, label, fg, bg, required, mandatory) -> ContrastCheck:  # noqa: ANN001
    return ContrastCheck(mode, label, fg, bg, contrast_ratio(fg, bg), required, mandatory)


def contrast_checks(tokens: dict) -> list[ContrastCheck]:
    """Measure the important colour pairs of a *complete* token set, in both modes.

    Mandatory (publishing is blocked when they fail): the main text on the page and on
    cards, text on primary/secondary buttons and text on the navigation.  Advisory
    (warnings): links, muted text, status text and the focus indicator - unusual but
    legitimate palettes may trip these, so the administrator decides.
    """
    checks: list[ContrastCheck] = []
    for mode in ("light", "dark"):
        c = tokens[mode]
        checks += [
            _check(mode, "Text on page background", c["foreground"], c["background"], WCAG_TEXT_AA, True),
            _check(mode, "Text on cards and panels", c["foreground"], c["surface"], WCAG_TEXT_AA, True),
            _check(mode, "Button text on primary", c["primary_foreground"], c["primary"], WCAG_TEXT_AA, True),
            _check(mode, "Button text on primary (hover)", c["primary_foreground"], c["primary_hover"], WCAG_TEXT_AA, True),
            _check(mode, "Button text on primary (pressed)", c["primary_foreground"], c["primary_active"], WCAG_TEXT_AA, True),
            _check(mode, "Button text on secondary", c["secondary_foreground"], c["secondary"], WCAG_TEXT_AA, True),
            _check(mode, "Navigation text", c["sidebar_foreground"], c["sidebar_background"], WCAG_TEXT_AA, True),
            _check(mode, "Links on page background", c["link"], c["background"], WCAG_TEXT_AA, False),
            _check(mode, "Links on cards and panels", c["link"], c["surface"], WCAG_TEXT_AA, False),
            _check(mode, "Muted text on cards and panels", c["muted_foreground"], c["surface"], WCAG_TEXT_AA, False),
            _check(mode, "Focus indicator on page background", c["focus"], c["background"], WCAG_NON_TEXT_AA, False),
        ]
        for status in ("success", "warning", "danger", "info", "neutral"):
            soft = mix(c[status], c["surface"], STATUS_SOFT_PERCENT)
            text = mix(c[status], c["foreground"], STATUS_TEXT_PERCENT)
            checks.append(
                _check(mode, f"{status.capitalize()} text on its tinted background", text, soft, WCAG_TEXT_AA, False)
            )
    return checks


def mandatory_failures(tokens: dict) -> list[ContrastCheck]:
    return [check for check in contrast_checks(tokens) if check.level == "error"]
