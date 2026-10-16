"""The answer-type engine: registry, configuration schemas, validation and the normalized value contract."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from apps.checklists import constants as c
from apps.checklists.answer_types import (
    ANSWERED,
    EMPTY,
    INVALID,
    NOT_APPLICABLE,
    AnswerContext,
    ConfigurationError,
    OptionRef,
    registry,
    validate_in_context,
)
from apps.checklists.answer_types.files import sniff_content_type, verify_content


def ctx(answer_type, config=None, *, options=(), required=True):
    definition = registry.get(answer_type)
    return AnswerContext(
        answer_type=answer_type,
        config=definition.clean_config(config or {}),
        options=tuple(OptionRef(id=str(uuid.uuid4()), value=v, label=v.title(), sort_order=i) for i, v in enumerate(options, 1)),
        required=required,
    )


def check(answer_type, raw, config=None, *, options=(), required=True, na=False):
    return validate_in_context(ctx(answer_type, config, options=options, required=required), raw, not_applicable=na)


def ok(answer_type, raw, expected, config=None, **kw):
    result = check(answer_type, raw, config, **kw)
    assert result.valid and result.status == ANSWERED, result.errors
    assert result.value == expected
    return result


def bad(answer_type, raw, code, config=None, **kw):
    result = check(answer_type, raw, config, **kw)
    assert result.status == INVALID and result.errors[0].code == code, (result.status, result.errors)
    return result


# ---------------------------------------------------------------------------
class TestRegistry:
    def test_every_database_answer_type_is_registered_exactly_once(self):
        assert sorted(registry.codes()) == sorted(c.AnswerType.values)
        assert len(registry.codes()) == len(set(registry.codes())) == 15

    def test_codes_labels_and_categories_are_complete(self):
        categories = {"choice", "value", "datetime", "media", "advanced"}
        for definition in registry.all():
            assert definition.code and definition.label and definition.description and definition.icon
            assert definition.category in categories

    def test_grouping_matches_the_builder_layout(self):
        grouped = {key: [d.code for d in defs] for key, _label, defs in registry.grouped()}
        assert grouped["choice"] == ["boolean", "yes_no", "yes_no_na", "single_choice", "multiple_choice"]
        assert grouped["value"] == ["score", "numeric", "text", "long_text"]
        assert grouped["datetime"] == ["date", "datetime"]
        assert grouped["media"] == ["photo", "file", "signature"]
        assert grouped["advanced"] == ["matrix"]
        assert [label for label, _ in registry.choices()] == ["Choice", "Value", "Date & Time", "Media", "Advanced"]

    def test_duplicate_registration_is_refused(self):
        with pytest.raises(ValueError):
            registry.register(type(registry.get("boolean")))

    def test_capabilities(self):
        caps = {d.code: d.capabilities for d in registry.all()}
        assert caps["single_choice"].requires_options and caps["multiple_choice"].requires_options
        assert not caps["boolean"].requires_options and not caps["score"].requires_options
        assert caps["yes_no_na"].has_fixed_options and caps["yes_no"].supports_options
        assert caps["score"].supports_score and not caps["numeric"].supports_score
        assert caps["numeric"].supports_numeric_validation and caps["score"].supports_numeric_validation
        assert caps["photo"].supports_evidence
        assert caps["photo"].is_media and caps["file"].is_media and caps["signature"].is_media
        assert not caps["multiple_choice"].produces_scalar and not caps["matrix"].produces_scalar
        assert all(caps[code].supports_rules for code in caps)
        assert all(not c_.supports_score for code, c_ in caps.items() if code != "score")

    def test_metadata_is_json_safe_and_complete(self):
        import json

        data = registry.describe(detail=True)
        json.dumps(data)
        by = {d["code"]: d for d in data}
        assert by["score"]["default_config"] == {"min": 1, "max": 5, "step": 1, "display": "numeric"}
        assert {f["key"] for f in by["numeric"]["config_schema"]} == {"min", "max", "decimals", "unit"}
        assert by["numeric"]["example_value"] == "4.5"

    def test_unknown_type(self):
        with pytest.raises(KeyError):
            registry.get("telepathy")
        with pytest.raises(ConfigurationError):
            registry.clean_config("telepathy", {})


# ---------------------------------------------------------------------------
class TestBooleanAndYesNo:
    @pytest.mark.parametrize("raw,expected", [(True, True), (False, False), ("true", True), ("FALSE", False), (1, True), (0, False)])
    def test_boolean_valid(self, raw, expected):
        assert ok("boolean", raw, expected).value is expected

    def test_false_is_an_answer_not_an_empty_value(self):
        assert check("boolean", False).status == ANSWERED

    @pytest.mark.parametrize("raw", ["maybe", 2, [], {}, 1.5])
    def test_boolean_rejects(self, raw):
        assert check("boolean", raw).status == INVALID

    @pytest.mark.parametrize("raw,expected", [("yes", "yes"), ("No", "no"), (" YES ", "yes")])
    def test_yes_no_valid(self, raw, expected):
        ok("yes_no", raw, expected)

    @pytest.mark.parametrize("raw", ["na", "maybe", True, 1, ["yes"]])
    def test_yes_no_rejects(self, raw):
        bad("yes_no", raw, "invalid_choice") if isinstance(raw, str) else bad("yes_no", raw, "type")

    @pytest.mark.parametrize("raw,expected", [("yes", "yes"), ("no", "no"), ("na", "na"), ("N/A", "na"), ("n/a", "na")])
    def test_yes_no_na_valid(self, raw, expected):
        ok("yes_no_na", raw, expected)

    def test_na_satisfies_a_required_item(self):
        assert check("yes_no_na", "na", required=True).status == ANSWERED

    def test_yes_no_na_rejects(self):
        bad("yes_no_na", "unknown", "invalid_choice")


# ---------------------------------------------------------------------------
class TestChoices:
    OPTIONS = ("excellent", "good", "poor")

    def test_single_valid_by_value_and_by_id(self):
        context = ctx("single_choice", options=self.OPTIONS)
        assert validate_in_context(context, "good").value == "good"
        assert validate_in_context(context, context.options[2].id).value == "poor"

    def test_single_rejects_unknown_foreign_and_wrong_shapes(self):
        bad("single_choice", "bogus", "invalid_option", options=self.OPTIONS)
        bad("single_choice", str(uuid.uuid4()), "invalid_option", options=self.OPTIONS)
        bad("single_choice", ["good"], "type", options=self.OPTIONS)
        bad("single_choice", 5, "type", options=self.OPTIONS)

    def test_multiple_valid_ordered_and_deduplicated(self):
        result = ok("multiple_choice", ["poor", "excellent", "poor"], ["excellent", "poor"], options=self.OPTIONS)
        assert result.serialized == ["excellent", "poor"]

    def test_multiple_rejects_invalid_option_and_shape(self):
        bad("multiple_choice", ["good", "nope"], "invalid_option", options=self.OPTIONS)
        bad("multiple_choice", "good", "type", options=self.OPTIONS)

    def test_multiple_min_and_max_selections(self):
        config = {"min_select": 2, "max_select": 2}
        bad("multiple_choice", ["good"], "too_few", config, options=self.OPTIONS)
        bad("multiple_choice", ["good", "poor", "excellent"], "too_many", config, options=self.OPTIONS)
        ok("multiple_choice", ["good", "poor"], ["good", "poor"], config, options=self.OPTIONS)

    def test_empty_selection_is_empty_not_invalid_when_optional(self):
        assert check("multiple_choice", [], options=self.OPTIONS, required=False).status == EMPTY
        assert check("multiple_choice", [], options=self.OPTIONS, required=True).errors[0].code == "required"

    def test_config_rules(self):
        with pytest.raises(ConfigurationError):
            registry.clean_config("multiple_choice", {"min_select": 3, "max_select": 2})
        assert registry.clean_config("single_choice", {}) == {"display": "radio"}


# ---------------------------------------------------------------------------
class TestScore:
    CFG = {"min": 1, "max": 5}

    def test_bounds_accepted(self):
        ok("score", 1, 1, self.CFG)
        ok("score", 5, 5, self.CFG)
        ok("score", "3", 3, self.CFG)
        ok("score", 3.0, 3, self.CFG)

    def test_out_of_range_rejected(self):
        bad("score", 0, "out_of_range", self.CFG)
        bad("score", 6, "out_of_range", self.CFG)
        assert "between 1 and 5" in check("score", 7, self.CFG).errors[0].message

    def test_zero_to_ten(self):
        ok("score", 0, 0, {"min": 0, "max": 10})
        ok("score", 10, 10, {"min": 0, "max": 10})

    def test_step(self):
        cfg = {"min": 0, "max": 10, "step": 5}
        ok("score", 5, 5, cfg)
        bad("score", 3, "invalid_step", cfg)

    @pytest.mark.parametrize("raw", [2.5, "x", True, [], None.__class__])
    def test_non_integers_rejected(self, raw):
        assert check("score", raw, self.CFG).status == INVALID

    def test_config_validation_and_defaults(self):
        assert registry.clean_config("score", {}) == {"min": 1, "max": 5, "step": 1, "display": "numeric"}
        assert registry.clean_config("score", {"min": 0, "max": 10, "default": 5, "display": "stars"})["default"] == 5
        for bad_config in ({"min": 5, "max": 1}, {"min": 1, "max": 5, "step": 3}, {"min": 1, "max": 500},
                           {"min": "a"}, {"display": "hologram"}, {"min": 1, "max": 5, "default": 9}, {"bogus": 1},
                           {"min": True}):
            with pytest.raises(ConfigurationError):
                registry.clean_config("score", bad_config)

    def test_answer_score_is_not_a_checklist_score(self):
        # The result is a plain value; nothing here (or anywhere in the engine) produces percentages or grades.
        result = check("score", 4, self.CFG)
        assert result.value == 4 and not hasattr(result, "percentage") and not hasattr(result, "grade")


# ---------------------------------------------------------------------------
class TestNumeric:
    def test_integer_and_decimal(self):
        ok("numeric", 4, Decimal("4.00"))
        ok("numeric", "4.5", Decimal("4.50"))
        ok("numeric", 4.5, Decimal("4.50"))

    def test_decimal_places_are_exact_not_float(self):
        cfg = {"decimals": 1}
        assert check("numeric", "4.5", cfg).serialized == "4.5"
        assert check("numeric", "4.50", cfg).serialized == "4.5"  # trailing zero is not extra precision
        bad("numeric", "4.55", "precision", cfg)
        assert check("numeric", Decimal("0.1") + Decimal("0.2"), {"decimals": 1}).serialized == "0.3"
        bad("numeric", "4.5", "precision", {"decimals": 0})
        ok("numeric", "5", Decimal("5"), {"decimals": 0})

    def test_minimum_and_maximum_with_unit_in_the_message(self):
        cfg = {"min": -10, "max": 20, "unit": "°C", "decimals": 1}
        ok("numeric", "-10", Decimal("-10.0"), cfg)
        ok("numeric", "20", Decimal("20.0"), cfg)
        assert "at least -10 °C" in bad("numeric", "-10.5", "out_of_range", cfg).errors[0].message
        assert "at most 20 °C" in bad("numeric", "20.1", "out_of_range", cfg).errors[0].message

    @pytest.mark.parametrize("raw", ["abc", "", "NaN", "Infinity", "1e999", True, [], {}])
    def test_invalid_input(self, raw):
        result = check("numeric", raw)
        assert result.status in (INVALID,) or (raw == "" and result.errors[0].code == "required")

    def test_units_are_free_text_not_hard_coded(self):
        for unit in ("°C", "°F", "kg", "g", "liter", "%", "minutes", "hours"):
            assert registry.clean_config("numeric", {"unit": unit})["unit"] == unit

    def test_serialization_is_a_decimal_string(self):
        assert check("numeric", "12", {"decimals": 2}).serialized == "12.00"

    def test_config_rules(self):
        for bad_config in ({"min": 9, "max": 1}, {"decimals": 7}, {"decimals": 1.5}, {"unit": "x" * 21}, {"min": "abc"}, {"extra": 1}):
            with pytest.raises(ConfigurationError):
                registry.clean_config("numeric", bad_config)
        assert registry.clean_config("numeric", {"min": "0", "max": "10.5"}) == {"min": 0, "max": 10.5, "decimals": 2}


# ---------------------------------------------------------------------------
class TestText:
    def test_lengths(self):
        cfg = {"min_length": 3, "max_length": 5}
        ok("text", "abc", "abc", cfg)
        ok("text", "abcde", "abcde", cfg)
        bad("text", "ab", "too_short", cfg)
        bad("text", "abcdef", "too_long", cfg)

    def test_whitespace_is_trimmed_before_measuring(self):
        ok("text", "  hi  ", "hi")
        assert check("text", "   ", required=True).errors[0].code == "required"

    def test_short_text_is_single_line_and_clean(self):
        bad("text", "a\nb", "multiline")
        bad("text", "a\x00b", "characters")
        bad("text", 5, "type")

    def test_long_text_keeps_lines(self):
        ok("long_text", "line one\r\nline two", "line one\nline two")
        bad("long_text", "x" * 6000, "too_long")
        ok("long_text", "x" * 6000, "x" * 6000, {"max_length": 6000})

    def test_markup_is_data_not_executed(self):
        # Validation does not sanitize HTML (output escaping does); it must at least survive unchanged.
        ok("text", "<script>alert(1)</script>", "<script>alert(1)</script>")

    def test_config(self):
        assert registry.clean_config("text", {}) == {"min_length": 0, "max_length": 255}
        assert registry.clean_config("long_text", {}) == {"min_length": 0, "max_length": 5000}
        for bad_config in ({"min_length": 9, "max_length": 3}, {"max_length": 0}, {"max_length": 20000}, {"x": 1}):
            with pytest.raises(ConfigurationError):
                registry.clean_config("long_text", bad_config)


# ---------------------------------------------------------------------------
class TestDates:
    def test_date_valid(self):
        result = ok("date", "2026-10-08", date(2026, 10, 8))
        assert result.serialized == "2026-10-08"

    @pytest.mark.parametrize("raw", ["2026-02-30", "08/10/2026", "yesterday", 20261008, datetime(2026, 1, 1, 12, 0)])
    def test_date_invalid(self, raw):
        assert check("date", raw).status == INVALID

    def test_date_range(self):
        cfg = {"min_date": "2026-01-01", "max_date": "2026-12-31"}
        ok("date", "2026-01-01", date(2026, 1, 1), cfg)
        bad("date", "2025-12-31", "out_of_range", cfg)
        bad("date", "2027-01-01", "out_of_range", cfg)

    def test_datetime_aware_is_normalized_to_utc(self):
        result = ok("datetime", "2026-10-08T14:30:00+05:00", datetime(2026, 10, 8, 9, 30, tzinfo=timezone.utc))
        assert result.serialized == "2026-10-08T09:30:00+00:00"

    def test_naive_datetime_uses_the_project_timezone(self, settings):
        from django.utils import timezone as dj_tz

        settings.TIME_ZONE = "Asia/Karachi"  # +05:00, no DST
        dj_tz.activate("Asia/Karachi")
        try:
            result = check("datetime", "2026-10-08T14:30:00")
        finally:
            dj_tz.deactivate()
        assert result.serialized == "2026-10-08T09:30:00+00:00"
        assert result.value.tzinfo is not None

    @pytest.mark.parametrize("raw", ["not a date", "2026-13-01T00:00:00", 5, date(2026, 1, 1)])
    def test_datetime_invalid(self, raw):
        assert check("datetime", raw).status == INVALID

    def test_datetime_range_and_config(self):
        cfg = {"min_datetime": "2026-01-01T00:00:00+00:00", "max_datetime": "2026-01-02T00:00:00+00:00"}
        ok("datetime", "2026-01-01T12:00:00+00:00", datetime(2026, 1, 1, 12, tzinfo=timezone.utc), cfg)
        bad("datetime", "2026-01-03T00:00:00+00:00", "out_of_range", cfg)
        with pytest.raises(ConfigurationError):
            registry.clean_config("datetime", {"min_datetime": "2026-02-01T00:00:00+00:00", "max_datetime": "2026-01-01T00:00:00+00:00"})
        with pytest.raises(ConfigurationError):
            registry.clean_config("date", {"min_date": "2026-02-01", "max_date": "2026-01-01"})
        with pytest.raises(ConfigurationError):
            registry.clean_config("date", {"min_date": "soon"})


# ---------------------------------------------------------------------------
class TestMatrix:
    CFG = {"rows": ["Dining Area", "Kitchen", "Restroom"], "columns": ["Clean", "Damaged", "Missing"]}

    def test_config_is_normalized_with_stable_keys(self):
        cfg = registry.clean_config("matrix", self.CFG)
        assert cfg["rows"][0] == {"key": "dining_area", "label": "Dining Area"}
        assert cfg["mode"] == "single" and cfg["require_all_rows"] is False
        assert registry.clean_config("matrix", cfg) == cfg  # idempotent: stored configs re-validate unchanged

    def test_valid_single_answer(self):
        result = ok("matrix", {"kitchen": "clean", "dining_area": "damaged"}, {"dining_area": "damaged", "kitchen": "clean"}, self.CFG)
        assert list(result.serialized) == ["dining_area", "kitchen"]  # definition order

    def test_invalid_row_column_and_cell_shapes(self):
        bad("matrix", {"garage": "clean"}, "invalid_row", self.CFG)
        bad("matrix", {"kitchen": "burnt"}, "invalid_column", self.CFG)
        bad("matrix", {"kitchen": ["clean"]}, "invalid_column", self.CFG)
        bad("matrix", ["kitchen"], "type", self.CFG)

    def test_required_cells(self):
        cfg = {**self.CFG, "require_all_rows": True}
        result = bad("matrix", {"kitchen": "clean"}, "row_required", cfg)
        assert "Dining Area" in result.errors[0].message
        ok("matrix", {"dining_area": "clean", "kitchen": "clean", "restroom": "missing"},
           {"dining_area": "clean", "kitchen": "clean", "restroom": "missing"}, cfg)

    def test_multiple_mode(self):
        cfg = {**self.CFG, "mode": "multiple"}
        ok("matrix", {"kitchen": ["missing", "clean", "clean"]}, {"kitchen": ["clean", "missing"]}, cfg)
        bad("matrix", {"kitchen": "clean"}, "type", cfg)
        bad("matrix", {"kitchen": ["clean", "zzz"]}, "invalid_column", cfg)

    def test_empty_matrix_follows_required(self):
        assert check("matrix", {}, self.CFG, required=False).status == EMPTY
        assert check("matrix", {"kitchen": ""}, self.CFG, required=True).errors[0].code == "required"

    @pytest.mark.parametrize("bad_config", [
        {}, {"rows": ["A"]}, {"rows": [], "columns": ["x"]}, {"rows": ["A", "a"], "columns": ["x"]},
        {"rows": [""], "columns": ["x"]}, {"rows": ["A"], "columns": ["x"], "mode": "diagonal"},
        {"rows": [{"key": "Bad Key", "label": "A"}], "columns": ["x"]}, {"rows": [{"label": "A", "extra": 1}], "columns": ["x"]},
        {"rows": ["r"] * 2, "columns": ["c"]},
    ])
    def test_bad_configs_rejected(self, bad_config):
        with pytest.raises(ConfigurationError):
            registry.clean_config("matrix", bad_config)

    def test_same_shape_for_no_restaurant_specific_model(self):
        warehouse = {"rows": ["Dock 1", "Dock 2"], "columns": ["Open", "Closed"]}
        assert ok("matrix", {"dock_1": "open"}, {"dock_1": "open"}, warehouse)


# ---------------------------------------------------------------------------
class TestMedia:
    FID = "0b6c7f0e-3d4a-4a46-9b8e-2f1e5d1c7a10"

    def file(self, name="shelf.jpg", size=1000, content_type=None, fid=None):
        entry = {"file_id": fid or str(uuid.uuid4()), "name": name, "size": size}
        if content_type:
            entry["content_type"] = content_type
        return entry

    def test_photo_valid_and_server_decides_the_type(self):
        result = ok("photo", [self.file(content_type="image/jpeg", fid=self.FID)],
                    [{"file_id": self.FID, "name": "shelf.jpg", "size": 1000, "content_type": "image/jpeg"}])
        assert result.serialized[0]["content_type"] == "image/jpeg"
        assert check("photo", self.file(fid=self.FID)).valid  # a single object is accepted

    def test_declared_type_must_match_the_extension(self):
        bad("photo", [self.file(name="a.png", content_type="image/jpeg")], "content_type")
        bad("file", [self.file(name="a.pdf", content_type="text/html")], "content_type")

    @pytest.mark.parametrize("name", ["evil.exe", "page.html", "x.php", "noext", "a.jpg.exe", "../a.jpg", "a/b.jpg", "a\\b.jpg", "a\x00.jpg", ""])
    def test_dangerous_or_malformed_names_rejected(self, name):
        assert check("photo", [self.file(name=name)]).status == INVALID

    def test_extension_allow_list_is_configurable_within_the_platform_universe(self):
        assert registry.clean_config("photo", {"allowed_extensions": [".JPG", "png"]})["allowed_extensions"] == ["jpg", "png"]
        for bad_config in ({"allowed_extensions": ["exe"]}, {"allowed_extensions": ["html"]}, {"allowed_extensions": []},
                           {"allowed_extensions": "jpg"}, {"max_files": 11}, {"max_size_mb": 0}, {"max_size_mb": 51}):
            with pytest.raises(ConfigurationError):
                registry.clean_config("photo", bad_config)
        bad("photo", [self.file(name="a.gif")], "extension")
        ok_cfg = {"allowed_extensions": ["png"]}
        bad("photo", [self.file(name="a.jpg")], "extension", ok_cfg)

    def test_size_and_count_limits(self):
        cfg = {"max_size_mb": 1, "max_files": 2}
        bad("photo", [self.file(size=1024 * 1024 + 1)], "too_large", cfg)
        ok_files = [self.file(), self.file()]
        assert check("photo", ok_files, cfg).valid
        bad("photo", [self.file(), self.file(), self.file()], "too_many", cfg)
        bad("photo", [self.file(size=0)], "size")
        bad("photo", [self.file(size="10")], "size")

    def test_file_reference_must_be_a_uuid_and_have_no_extra_fields(self):
        bad("photo", [{"file_id": "not-a-uuid", "name": "a.jpg", "size": 1}], "file_id")
        bad("photo", [{**self.file(), "url": "http://evil"}], "unknown_field")
        bad("photo", ["a.jpg"], "type")

    def test_duplicate_references_collapse(self):
        same = self.file(fid=self.FID)
        assert len(check("photo", [same, same], {"max_files": 2}).value) == 1

    def test_file_type_defaults(self):
        assert check("file", [self.file(name="report.pdf")]).valid
        assert check("file", [self.file(name="sheet.xlsx")]).valid
        assert check("file", [self.file(name="x.html")]).status == INVALID

    def test_content_sniffing_contract(self):
        assert verify_content("jpg", b"\xff\xd8\xff\xe0" + b"0" * 12)
        assert verify_content("png", b"\x89PNG\r\n\x1a\n" + b"0" * 8)
        assert verify_content("pdf", b"%PDF-1.7\n")
        assert verify_content("docx", b"PK\x03\x04" + b"0" * 12)
        assert verify_content("xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        assert verify_content("txt", b"hello")
        assert not verify_content("jpg", b"%PDF-1.7\n")  # renamed PDF
        assert not verify_content("png", b"MZ\x90\x00")  # an executable called .png
        assert not verify_content("txt", b"ab\x00cd")
        assert sniff_content_type(b"GIF89a") is None

    def test_signature(self):
        ok("signature", {"signer_name": "  A. Khan "}, {"signer_name": "A. Khan", "file_id": None})
        ok("signature", {"signer_name": "A", "file_id": self.FID}, {"signer_name": "A", "file_id": self.FID})
        bad("signature", {"file_id": self.FID}, "signer_name")
        ok("signature", {"file_id": self.FID}, {"signer_name": "", "file_id": self.FID}, {"require_name": False})
        bad("signature", {"signer_name": "", "file_id": None}, "signer_name")
        bad("signature", {"signer_name": "A", "file_id": "nope"}, "file_id")
        bad("signature", {"signer_name": "A", "x": 1}, "unknown_field")
        bad("signature", "A. Khan", "type")
        assert registry.clean_config("signature", {}) == {"signer_label": "Signer", "require_name": True}


# ---------------------------------------------------------------------------
class TestRequiredOptionalNotApplicable:
    def test_required_empty_is_invalid(self):
        for answer_type in ("yes_no", "score", "numeric", "text", "date", "photo", "matrix"):
            config = {"rows": ["r"], "columns": ["c"]} if answer_type == "matrix" else None
            assert check(answer_type, None, config, required=True).errors[0].code == "required", answer_type

    def test_optional_empty_is_valid_and_empty(self):
        for answer_type in ("yes_no", "score", "numeric", "text", "date", "photo", "signature"):
            result = check(answer_type, "", required=False) if answer_type != "photo" else check(answer_type, [], required=False)
            assert result.valid and result.status == EMPTY and result.value is None, answer_type

    def test_not_applicable_is_valid_and_distinct(self):
        result = check("numeric", None, required=True, na=True)
        assert result.valid and result.status == NOT_APPLICABLE and result.value is None
        assert check("yes_no_na", "na").status == ANSWERED  # N/A as a *value* is an answer

    def test_not_applicable_ignores_a_supplied_value(self):
        assert check("score", 99, {"min": 1, "max": 5}, na=True).status == NOT_APPLICABLE

    def test_zero_is_not_empty(self):
        assert check("numeric", 0).status == ANSWERED
        assert check("score", 0, {"min": 0, "max": 5}).status == ANSWERED

    def test_rule_engine_can_override_required(self):
        base = ctx("text", required=False)
        required_now = AnswerContext(base.answer_type, base.config, base.options, required=True)
        assert validate_in_context(base, "").status == EMPTY
        assert validate_in_context(required_now, "").status == INVALID

    def test_unknown_stored_type_is_reported_not_crashed(self):
        result = validate_in_context(AnswerContext(answer_type="telepathy"), "x")
        assert result.errors[0].code == "unknown_type"

    def test_results_serialize_to_plain_json(self):
        import json

        json.dumps(check("numeric", "4.5", {"decimals": 1}).as_dict())
        json.dumps(check("datetime", "2026-10-08T10:00:00+00:00").as_dict())
        assert check("score", 7, {"min": 1, "max": 5}).as_dict()["errors"][0]["code"] == "out_of_range"


# ---------------------------------------------------------------------------
class TestConfigurationGeneral:
    @pytest.mark.parametrize("answer_type", ["boolean", "yes_no", "yes_no_na"])
    def test_types_without_settings_reject_any_key(self, answer_type):
        assert registry.clean_config(answer_type, {}) == {}
        with pytest.raises(ConfigurationError):
            registry.clean_config(answer_type, {"min": 1})

    def test_non_object_configuration_rejected(self):
        for raw in ("nope", [1], 5):
            with pytest.raises(ConfigurationError):
                registry.clean_config("score", raw)

    def test_clean_config_is_idempotent_for_every_type(self):
        samples = {"matrix": {"rows": ["a"], "columns": ["b"]}}
        for definition in registry.all():
            cleaned = definition.clean_config(samples.get(definition.code, {}))
            assert definition.clean_config(cleaned) == cleaned, definition.code

    def test_legacy_phase4_configs_still_read(self):
        assert registry.get("score").config_with_defaults({"min": 1, "max": 10}) == {"min": 1, "max": 10, "step": 1, "display": "numeric"}
        assert registry.get("numeric").config_with_defaults({"min": 0, "max": 8, "unit": "°C"})["decimals"] == 2

    def test_django_wrapper_reports_field_errors(self):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError) as info:
            c.clean_config("score", {"min": 9, "max": 1})
        assert "config" in info.value.message_dict
        with pytest.raises(ValidationError) as info:
            c.clean_config("telepathy", {})
        assert "answer_type" in info.value.message_dict
