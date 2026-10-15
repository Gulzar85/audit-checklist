"""Built-in theme presets: named sets of token overrides.

A preset is *data* in the token vocabulary - not templates, not CSS.  "Create theme from
preset" copies the preset's overrides into a new draft theme.  Add a preset by appending to
``PRESETS``; ``test_presets`` validates every entry (token names, values, WCAG mandatory
contrast) so a bad preset cannot ship.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .constants import Appearance


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    description: str
    default_appearance: str = Appearance.SYSTEM
    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)


_CORPORATE = Preset(
    key="corporate",
    name="Corporate",
    description="Deep navy brand colour, a dark navigation bar and restrained corners.",
    tokens={
        "light": {
            "primary": "#0b3d91",
            "primary_hover": "#082f6f",
            "primary_active": "#062456",
            "accent": "#8a5a00",
            "link": "#0b3d91",
            "focus": "#2563eb",
            "info": "#0b3d91",
            "sidebar_background": "#0b2447",
            "sidebar_foreground": "#e2e8f0",
            "sidebar_active": "#ffffff",
            "sidebar_hover": "#163563",
            "sidebar_border": "#0b2447",
        },
        "dark": {
            "primary": "#7aa7ff",
            "primary_hover": "#9dbfff",
            "primary_active": "#bfd4ff",
            "primary_foreground": "#0b1220",
            "accent": "#f5c451",
            "link": "#9dbfff",
            "focus": "#9dbfff",
            "info": "#7aa7ff",
            "sidebar_background": "#07152e",
            "sidebar_border": "#163563",
            "sidebar_hover": "#163563",
        },
        "shared": {
            "font_family": "humanist",
            "heading_weight": "700",
            "radius_sm": 3,
            "radius_md": 5,
            "radius_lg": 8,
        },
    },
)

_MINIMAL = Preset(
    key="minimal",
    name="Minimal",
    description="Monochrome, square-cornered, flat and compact.",
    tokens={
        "light": {
            "primary": "#18181b",
            "primary_hover": "#27272a",
            "primary_active": "#3f3f46",
            "accent": "#3f3f46",
            "link": "#18181b",
            "focus": "#52525b",
            "info": "#3f3f46",
            "background": "#fafafa",
            "muted": "#f4f4f5",
            "border": "#d4d4d8",
            "sidebar_active": "#18181b",
            "sidebar_hover": "#f4f4f5",
        },
        "dark": {
            "primary": "#e4e4e7",
            "primary_hover": "#fafafa",
            "primary_active": "#ffffff",
            "primary_foreground": "#18181b",
            "accent": "#d4d4d8",
            "link": "#e4e4e7",
            "focus": "#d4d4d8",
            "info": "#d4d4d8",
            "background": "#09090b",
            "surface": "#18181b",
            "surface_elevated": "#27272a",
            "muted": "#27272a",
            "border": "#3f3f46",
            "sidebar_background": "#18181b",
            "sidebar_hover": "#27272a",
            "sidebar_border": "#27272a",
            "sidebar_active": "#e4e4e7",
        },
        "shared": {
            "radius_sm": 0,
            "radius_md": 2,
            "radius_lg": 4,
            "shadow_sm": "none",
            "shadow_md": "none",
            "shadow_lg": "subtle",
            "density": "compact",
            "component_padding": "compact",
        },
    },
)

_HIGH_CONTRAST = Preset(
    key="high_contrast",
    name="High contrast",
    description="Maximum text and border contrast, strong focus indicators, no shadows. Status never relies on colour alone.",
    tokens={
        "light": {
            "primary": "#0000b3",
            "primary_hover": "#00008a",
            "primary_active": "#000066",
            "primary_foreground": "#ffffff",
            "secondary": "#ffffff",
            "secondary_foreground": "#000000",
            "accent": "#5b0099",
            "link": "#0000cc",
            "focus": "#cc4400",
            "background": "#ffffff",
            "surface": "#ffffff",
            "surface_elevated": "#ffffff",
            "foreground": "#000000",
            "muted": "#f0f0f0",
            "muted_foreground": "#1a1a1a",
            "border": "#000000",
            "success": "#005c26",
            "warning": "#7a4100",
            "danger": "#a60019",
            "info": "#003f73",
            "neutral": "#262626",
            "sidebar_background": "#000000",
            "sidebar_foreground": "#ffffff",
            "sidebar_active": "#ffd400",
            "sidebar_hover": "#333333",
            "sidebar_border": "#000000",
        },
        "dark": {
            "primary": "#ffd400",
            "primary_hover": "#ffe55c",
            "primary_active": "#fff08a",
            "primary_foreground": "#000000",
            "secondary": "#000000",
            "secondary_foreground": "#ffffff",
            "accent": "#7ee8ff",
            "link": "#99ccff",
            "focus": "#ffd400",
            "background": "#000000",
            "surface": "#000000",
            "surface_elevated": "#141414",
            "foreground": "#ffffff",
            "muted": "#1a1a1a",
            "muted_foreground": "#e6e6e6",
            "border": "#ffffff",
            "success": "#66ff9e",
            "warning": "#ffd400",
            "danger": "#ff9494",
            "info": "#99d9ff",
            "neutral": "#e0e0e0",
            "sidebar_background": "#000000",
            "sidebar_foreground": "#ffffff",
            "sidebar_active": "#ffd400",
            "sidebar_hover": "#262626",
            "sidebar_border": "#ffffff",
        },
        "shared": {
            "font_size_base": 17,
            "line_height": 1.6,
            "heading_weight": "800",
            "body_weight": "500",
            "radius_sm": 2,
            "radius_md": 4,
            "radius_lg": 6,
            "shadow_sm": "none",
            "shadow_md": "none",
            "shadow_lg": "none",
            "component_padding": "relaxed",
        },
    },
)

_MCDONALDS = Preset(
    key="mcdonalds",
    name="McDonald's",
    description="Golden Arches red and yellow: a red navigation bar and primary actions in light mode, a yellow accent on charcoal in dark mode.",
    tokens={
        "light": {
            "primary": "#da291c",
            "primary_hover": "#b71f14",
            "primary_active": "#9a190f",
            "accent": "#ffc72c",
            "link": "#b71f14",
            "focus": "#27251f",
            "sidebar_background": "#da291c",
            "sidebar_foreground": "#ffffff",
            "sidebar_active": "#ffc72c",
            "sidebar_hover": "#b71f14",
            "sidebar_border": "#b71f14",
        },
        "dark": {
            "primary": "#ffc72c",
            "primary_hover": "#ffd65c",
            "primary_active": "#ffe08a",
            "primary_foreground": "#27251f",
            "accent": "#ffc72c",
            "link": "#ffc72c",
            "focus": "#ffc72c",
            "background": "#121212",
            "surface": "#1e1e1e",
            "surface_elevated": "#2a2a2a",
            "muted": "#2a2a2a",
            "border": "#3d3d3d",
            "sidebar_background": "#1a1a1a",
            "sidebar_foreground": "#ffffff",
            "sidebar_active": "#ffc72c",
            "sidebar_hover": "#2e2e2e",
            "sidebar_border": "#da291c",
        },
    },
)

PRESETS: dict[str, Preset] = {
    preset.key: preset
    for preset in (
        Preset(
            key="default",
            name="Default",
            description="The platform's built-in look. An empty override set.",
        ),
        _CORPORATE,
        Preset(
            key="dark",
            name="Dark",
            description="The default palette, opening in dark mode unless a user chooses otherwise.",
            default_appearance=Appearance.DARK,
        ),
        _HIGH_CONTRAST,
        _MINIMAL,
        _MCDONALDS,
    )
}


def get_preset(key: str) -> Preset | None:
    return PRESETS.get(key)


def preset_choices() -> list[tuple[str, str]]:
    return [(preset.key, preset.name) for preset in PRESETS.values()]
