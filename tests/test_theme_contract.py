"""The frontend contract: components consume semantic tokens, never raw brand colours."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings

from apps.themes import css
from apps.themes import tokens as tk

ROOT = Path(settings.BASE_DIR)
PALETTE = (
    r"(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|"
    r"violet|purple|fuchsia|pink|rose|brand)"
)
RAW_COLOR = re.compile(
    rf"(?<![\w-])(?:[a-z0-9-]+:)*(?:bg|text|border|divide|ring|from|to|via|fill|stroke|outline|accent|shadow|decoration)"
    rf"-(?:{PALETTE}-\d{{2,3}}|white|black)\b"
)
ARBITRARY_COLOR = re.compile(r"(?:bg|text|border|ring)-\[#[0-9a-fA-F]{3,8}\]")


def project_files(*patterns: str) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        files += [p for p in ROOT.glob(pattern) if p.is_file()]
    return files


TEMPLATES = project_files("templates/**/*.html")
SOURCES = project_files("apps/**/*.py", "static/js/app.js", "static/js/components.js")


class TestNoRawColoursInBusinessUi:
    @pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: str(p.relative_to(ROOT)))
    def test_templates_use_semantic_classes_only(self, path):
        text = path.read_text(encoding="utf-8")
        assert RAW_COLOR.findall(text) == [], f"raw palette class in {path.name}"
        assert ARBITRARY_COLOR.findall(text) == [], f"arbitrary hex colour in {path.name}"

    @pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
    def test_python_and_js_do_not_emit_raw_palette_classes(self, path):
        text = path.read_text(encoding="utf-8")
        assert RAW_COLOR.findall(text) == []

    def test_templates_do_not_need_dark_variants(self):
        offenders = [p.name for p in TEMPLATES if re.search(r"(?<![\w-])dark:[a-z]", p.read_text(encoding="utf-8"))]
        assert offenders == []  # tokens switch with the mode; `dark:` would bypass the theme

    def test_templates_have_no_inline_style_attributes(self):
        offenders = [p.name for p in TEMPLATES if re.search(r"<[a-z][^>]*\sstyle=", p.read_text(encoding="utf-8"))]
        assert offenders == []

    def test_tailwind_source_has_no_raw_palette(self):
        source = (ROOT / "tailwind" / "input.css").read_text(encoding="utf-8")
        assert RAW_COLOR.findall(source) == []
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", source), "hex colours belong in the token registry"

    def test_primary_button_is_wired_to_tokens(self):
        source = (ROOT / "tailwind" / "input.css").read_text(encoding="utf-8")
        assert "bg-primary text-primary-foreground hover:bg-primary-hover active:bg-primary-active" in source
        assert "var(--button-radius)" in source and "var(--card-radius)" in source


@pytest.fixture(scope="module")
def built():
    return (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")


class TestBuiltStylesheet:
    @staticmethod
    def minified(value):
        """The build shortens ``#aabbcc`` to ``#abc``."""
        match = re.fullmatch(r"#(.)\1(.)\2(.)\3", value)
        return "#" + "".join(match.groups()) if match else value

    def test_every_token_default_is_in_the_build(self, built):
        """Fails when someone changes the registry but forgets ``npm run css:build``."""
        light = tk.default_tokens()["light"]
        for key, value in light.items():
            assert f"{tk.TOKENS[key].css}:{self.minified(value)}" in built, f"{key} missing - run render_theme_defaults + css:build"

    def test_semantic_utilities_resolve_through_variables(self, built):
        for rule in (
            ".bg-primary{background-color:var(--color-primary)}",
            ".bg-surface{background-color:var(--color-surface)}",
            ".text-foreground{color:var(--color-foreground)}",
            ".border-border{border-color:var(--color-border)}",
            ".text-muted-foreground{color:var(--color-muted-foreground)}",
            ".rounded-lg{border-radius:var(--radius-lg)}",
        ):
            assert rule in built, rule

    def test_dark_and_system_rules_exist(self, built):
        assert "[data-theme=dark]" in built and "[data-theme=system]" in built
        assert "prefers-color-scheme:dark" in built

    def test_reduced_motion_is_respected(self, built):
        assert "prefers-reduced-motion:reduce" in built

    def test_spacing_follows_the_density_token(self, built):
        assert re.search(r"--spacing:calc\(\.25rem\s*\*\s*var\(--spacing-scale\)\)", built)

    def test_derived_status_tints_are_in_the_build(self, built):
        for name in css.derived_variables():
            assert name in built


class TestResponsive:
    def test_editor_adapts_from_phone_to_desktop(self):
        text = (ROOT / "templates" / "themes" / "theme_edit.html").read_text(encoding="utf-8")
        assert "xl:grid-cols-" in text and "sm:grid-cols-" in text and "flex-wrap" in text

    def test_navigation_keeps_its_mobile_menu(self):
        text = (ROOT / "templates" / "partials" / "navbar.html").read_text(encoding="utf-8")
        assert 'id="mobile-nav"' in text and "lg:hidden" in text


class TestDocumentation:
    @pytest.mark.parametrize(
        "name", ["architecture", "design-tokens", "theme-resolution", "security", "accessibility", "frontend-integration"]
    )
    def test_theme_docs_exist(self, name):
        path = ROOT / "docs" / "themes" / f"{name}.md"
        assert path.exists() and len(path.read_text(encoding="utf-8")) > 800

    def test_adrs_exist(self):
        adrs = sorted((ROOT / "docs" / "decisions").glob("00[2-3]*.md"))
        titles = " ".join(p.read_text(encoding="utf-8") for p in adrs).lower()
        for topic in ("css variables", "semantic", "ownership", "precedence", "validation", "arbitrary", "tailwind", "light"):
            assert topic in titles, topic
