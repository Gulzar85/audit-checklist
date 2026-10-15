"""The token registry: the allow-list that makes theming safe by construction."""

from __future__ import annotations

import re

import pytest
from django.core.exceptions import ValidationError

from apps.themes import contrast, css, presets
from apps.themes import tokens as tk
from apps.themes.constants import STATUS_SOFT_PERCENT
from apps.themes.management.commands.render_theme_defaults import TARGET


class TestColors:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("#DA291C", "#da291c"),
            ("#fff", "#ffffff"),
            ("  #AbC  ", "#aabbcc"),
            ("rgb(218, 41, 28)", "#da291c"),
            ("rgb(218 41 28)", "#da291c"),
            ("rgba(218, 41, 28, 1)", "#da291c"),
            ("hsl(0, 100%, 50%)", "#ff0000"),
            ("hsl(120 100% 25%)", "#008000"),
            ("hsla(240, 100%, 50%, 1)", "#0000ff"),
        ],
    )
    def test_approved_formats_are_normalised_to_hex(self, raw, expected):
        assert tk.normalize_color(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "red",  # named colours are not an approved format
            "#12",
            "#12345",
            "#1234567",
            "#ffffff80",  # alpha is not supported
            "rgba(0, 0, 0, 0.5)",
            "hsla(0, 0%, 0%, 50%)",
            "rgb(300, 0, 0)",
            "hsl(0, 200%, 50%)",
            "var(--x)",
            "url(https://evil.example/x.png)",
            "expression(alert(1))",
            "#fff; background: url(x)",
            "#fff} body{display:none",
            "javascript:alert(1)",
            "<script>alert(1)</script>",
            "@import 'x'",
            "",
            "x" * 500,
            None,
            123,
            ["#fff"],
        ],
    )
    def test_everything_else_is_rejected(self, raw):
        with pytest.raises(ValidationError):
            tk.normalize_color(raw)


class TestValues:
    def test_numbers_are_range_checked(self):
        assert tk.validate_token_value("font_size_base", 16) == 16
        for bad in (11, 21, "abc", True, float("nan"), float("inf"), None, [], "1e9"):
            with pytest.raises(ValidationError):
                tk.validate_token_value("font_size_base", bad)

    def test_numeric_strings_from_forms_are_accepted(self):
        assert tk.validate_token_value("line_height", "1.6") == 1.6

    def test_choices_are_closed(self):
        assert tk.validate_token_value("font_family", "serif") == "serif"
        assert tk.validate_token_value("heading_weight", 700) == "700"
        for bad in ("Comic Sans", "https://fonts.example/x.woff", "serif; color:red", 5, None):
            with pytest.raises(ValidationError):
                tk.validate_token_value("font_family", bad)

    def test_unknown_token_is_rejected(self):
        with pytest.raises(ValidationError):
            tk.validate_token_value("custom_css", "body{}")


class TestDocument:
    def test_valid_document_is_canonicalised(self):
        clean = tk.validate_tokens({"light": {"primary": "#ABC"}, "shared": {"radius_md": "10"}})
        assert clean == {"light": {"primary": "#aabbcc"}, "shared": {"radius_md": 10}}

    @pytest.mark.parametrize(
        "document",
        [
            {"custom_css": "body{}"},
            {"light": {"custom_css": "body{}"}},
            {"light": {"primary": "red"}},
            {"shared": {"primary": "#fff"}},  # mode token in the shared section
            {"light": {"radius_md": 4}},  # shared token in a mode section
            {"html": "<b>x</b>"},
            {"light": "not-an-object"},
            [],
            "string",
            {"light": {"primary": "#fff", "<script>": "x"}},
        ],
    )
    def test_malformed_documents_are_rejected(self, document):
        with pytest.raises(ValidationError):
            tk.validate_tokens(document)

    def test_empty_is_fine(self):
        assert tk.validate_tokens(None) == {}
        assert tk.validate_tokens({}) == {}

    def test_all_problems_are_reported_together(self):
        with pytest.raises(ValidationError) as caught:
            tk.validate_tokens({"light": {"primary": "red", "link": "blue"}, "zzz": {}})
        assert len(caught.value.message_dict["tokens"]) == 3


class TestLayering:
    def test_defaults_are_complete_and_valid(self):
        defaults = tk.default_tokens()
        assert set(defaults["light"]) == {t.key for t in tk.MODE_TOKENS}
        assert set(defaults["dark"]) == {t.key for t in tk.MODE_TOKENS}
        assert set(defaults["shared"]) == {t.key for t in tk.SHARED_TOKENS}
        assert tk.validate_tokens(defaults) == defaults

    def test_later_layers_override_earlier_ones(self):
        merged = tk.merge_tokens({"light": {"primary": "#111111"}}, {"light": {"primary": "#222222"}})
        assert merged["light"]["primary"] == "#222222"
        assert merged["dark"]["primary"] == tk.TOKENS["primary"].dark

    def test_merge_does_not_mutate_defaults(self):
        tk.merge_tokens({"light": {"primary": "#111111"}})
        assert tk.default_tokens()["light"]["primary"] == tk.TOKENS["primary"].light

    def test_diff_reports_old_and_new(self):
        diff = tk.diff_tokens({"light": {"primary": "#111111"}}, {"light": {"primary": "#222222"}})
        assert diff == [
            {"token": "primary", "label": "Primary (light)", "section": "light", "from": "#111111", "to": "#222222"}
        ]

    def test_registry_names_are_semantic(self):
        banned = ("red", "blue", "green", "yellow", "orange", "purple", "pink")
        for key in tk.TOKENS:
            assert not any(color in key for color in banned), key

    def test_registry_is_modest(self):
        assert len(tk.TOKENS) < 70  # "do not create hundreds of tokens"


class TestEmission:
    def test_only_custom_property_declarations_are_emitted(self):
        stylesheet = css.compile_css(tk.default_tokens())
        body = re.sub(r"/\*.*?\*/", "", stylesheet, flags=re.S)
        for line in body.splitlines():
            line = line.strip()
            if not line or line.endswith("{") or line == "}":
                continue
            assert re.fullmatch(r"(--[a-z0-9-]+|color-scheme): [^;{}<>@]+;", line), line

    def test_no_selector_or_at_rule_can_come_from_values(self):
        hostile = tk.merge_tokens({"light": {"primary": "#ff0000"}, "shared": {"font_family": "mono"}})
        stylesheet = css.compile_css(hostile)
        assert "@import" not in stylesheet
        assert "url(" not in stylesheet
        assert "javascript:" not in stylesheet
        assert "<" not in stylesheet
        assert stylesheet.count("@media") == 1

    def test_three_blocks_for_light_dark_and_system(self):
        stylesheet = css.compile_css(tk.default_tokens())
        assert ':root, [data-theme="light"] {' in stylesheet
        assert '[data-theme="dark"] {' in stylesheet
        assert '(prefers-color-scheme: dark)' in stylesheet
        assert '[data-theme="system"] {' in stylesheet

    def test_semantic_variable_names(self):
        stylesheet = css.compile_css(tk.default_tokens())
        for name in ("--color-primary", "--color-primary-hover", "--color-surface", "--color-foreground",
                     "--color-border", "--color-success", "--color-warning", "--color-danger",
                     "--radius-sm", "--radius-md", "--radius-lg", "--shadow-sm", "--font-family"):
            assert f"{name}:" in stylesheet

    def test_px_values_become_rem(self):
        assert tk.emit(tk.TOKENS["radius_md"], 8) == {"--radius-md": "0.5rem"}
        assert tk.emit(tk.TOKENS["radius_sm"], 0) == {"--radius-sm": "0"}

    def test_status_foregrounds_are_derived_for_readability(self):
        variables = tk.mode_variables(tk.default_tokens(), "light")
        for status in tk.STATUS_NAMES:
            background = tk.default_tokens()["light"][status]
            assert contrast.contrast_ratio(variables[f"--color-{status}-foreground"], background) >= 4.5

    def test_committed_tailwind_defaults_match_the_registry(self):
        assert TARGET.read_text(encoding="utf-8") == css.compile_defaults_for_tailwind()

    def test_derived_status_tints_use_the_shared_percentages(self):
        derived = css.derived_variables()
        assert f"{STATUS_SOFT_PERCENT}%" in derived["--color-success-soft"]


class TestContrastMaths:
    def test_black_on_white_is_21(self):
        assert round(contrast.contrast_ratio("#000000", "#ffffff"), 2) == 21.0

    def test_same_colour_is_1(self):
        assert contrast.contrast_ratio("#777777", "#777777") == pytest.approx(1.0)

    def test_mix_matches_browser_arithmetic(self):
        assert contrast.mix("#000000", "#ffffff", 50) == "#808080"
        assert contrast.mix("#ff0000", "#0000ff", 100) == "#ff0000"

    def test_best_foreground(self):
        assert contrast.best_foreground("#ffffff") == contrast.DARK_TEXT
        assert contrast.best_foreground("#000000") == contrast.LIGHT_TEXT


class TestPresets:
    @pytest.mark.parametrize("key", list(presets.PRESETS))
    def test_every_preset_is_valid_and_accessible(self, key):
        preset = presets.PRESETS[key]
        assert tk.validate_tokens(preset.tokens) == preset.tokens
        merged = tk.merge_tokens(preset.tokens)
        assert contrast.mandatory_failures(merged) == []

    def test_expected_presets_exist(self):
        assert {"default", "corporate", "dark", "high_contrast", "minimal", "mcdonalds"} <= set(presets.PRESETS)

    def test_high_contrast_passes_every_check_in_both_modes(self):
        merged = tk.merge_tokens(presets.PRESETS["high_contrast"].tokens)
        assert [c for c in contrast.contrast_checks(merged) if not c.passed] == []

    def test_high_contrast_has_stronger_text_contrast_than_default(self):
        default = tk.default_tokens()
        strong = tk.merge_tokens(presets.PRESETS["high_contrast"].tokens)
        for mode in ("light", "dark"):
            assert contrast.contrast_ratio(strong[mode]["foreground"], strong[mode]["background"]) > contrast.contrast_ratio(
                default[mode]["foreground"], default[mode]["background"]
            )


class TestMcDonaldsPreset:
    def test_brand_colours_and_full_accessibility(self):
        preset = presets.PRESETS["mcdonalds"]
        merged = tk.merge_tokens(preset.tokens)
        assert merged["light"]["primary"] == "#da291c" and merged["dark"]["primary"] == "#ffc72c"
        assert [c for c in contrast.contrast_checks(merged) if not c.passed] == []
