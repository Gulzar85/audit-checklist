"""The notification registries, template security, filters and retry arithmetic (Phase 17) - pure unit tests, no database."""

from __future__ import annotations

import pytest

from apps.notifications import constants as k
from apps.notifications import filters, registry, templating
from apps.notifications.providers.base import EmailProviderError
from apps.notifications.providers.microsoft365 import Microsoft365EmailProvider
from apps.notifications.services.base import mask_email, valid_address
from apps.notifications.services.delivery import backoff_seconds


# ---------------------------------------------------------------------------------------------------------------------------------- registry
class TestRegistry:
    def test_every_event_is_consistent(self):
        for code, event in registry.EVENTS.items():
            assert code == event.code and event.source_type in registry.SOURCES and event.category in k.Category.values
            assert set(event.default_rule.channels) <= set(k.Channel.values) and event.default_rule.policy in k.PreferencePolicy.values
            fields = {"subject": event.subject, "body_text": event.body, "in_app_title": event.title, "in_app_message": event.message}
            assert templating.validate(fields, event.variables) == [], code  # the built-in default text is valid for its own event
            assert {s["type"] for s in event.default_rule.strategies} <= set(k.Strategy.values)
            for flt in event.default_rule.filters:
                assert filters.clean_filters(event, [dict(flt)])[1] == [], code

    def test_event_codes_are_unique_dotted_identifiers(self):
        assert all(code.count(".") == 1 and code == code.lower() for code in registry.EVENTS)

    def test_describe_all_lists_every_registry(self):
        d = registry.describe_all()
        assert {e["code"] for e in d["events"]} == set(registry.EVENTS) and {s["value"] for s in d["strategies"]} == set(k.Strategy.values)

    def test_links_are_internal_paths_only(self):
        assert registry.absolute_link("/findings/1/") == "/findings/1/"
        assert registry.absolute_link("https://evil.example/") == "" and registry.absolute_link("//evil.example/x") == "" and registry.absolute_link("javascript:alert(1)") == ""

    def test_the_base_url_is_prefixed_to_a_path(self, settings):
        settings.NOTIFICATION_BASE_URL = "https://app.example.com/"
        assert registry.absolute_link("/findings/1/") == "https://app.example.com/findings/1/"


# ---------------------------------------------------------------------------------------------------------------------------------- templates
ALLOWED = registry.EVENTS["corrective_action.assigned"].variables
FULL = {"subject": "Action: {{ title }}", "body_text": "Due {{ due_date }}: {{ link }}", "in_app_title": "Action", "in_app_message": "{{ title }} due {{ due_date }}"}


class TestTemplateSecurity:
    def test_a_valid_template_validates_and_renders_with_synthetic_values(self):
        assert templating.validate(FULL, ALLOWED) == []
        out = templating.render(FULL, templating.sample_context(ALLOWED))
        assert out["subject"] == "Action: Sample title" and "01 Jan 2030" in out["body_text"]

    @pytest.mark.parametrize("text", ["{% load static %}", "{% if x %}a{% endif %}", "{# comment #}", "{{ title|upper }}", "{{ title.__class__ }}", "{{ request.user.password }}",
                                      "{{ settings.SECRET_KEY }}", "{{ 1+1 }}", "{{ title|safe }}", "{{ __import__('os') }}", "{{ title }", "{{ }}", "{{ Title }}"])
    def test_anything_but_a_plain_allowed_variable_is_refused(self, text):
        assert templating.validate({**FULL, "subject": text}, ALLOWED)

    def test_unknown_variables_are_listed_with_the_allowed_ones(self):
        problems = templating.validate({**FULL, "body_text": "{{ secret_answer }}"}, ALLOWED)
        assert problems and "secret_answer" in problems[0] and "title" in problems[0]

    def test_a_template_needs_a_complete_channel_set(self):
        assert templating.validate({"subject": "x"}, ALLOWED)  # no body, no in-app text
        assert templating.validate({"in_app_title": "t", "in_app_message": "m"}, ALLOWED) == []  # in-app only is fine
        assert templating.validate({"subject": "s", "body_text": "b", "body_html": "<p>x</p>"}, ALLOWED) == []
        assert templating.validate({"subject": "s", "body_html": "<p>x</p>"}, ALLOWED)  # an HTML body needs the plain-text fallback

    def test_length_limits(self):
        assert templating.validate({**FULL, "subject": "x" * 201}, ALLOWED) and templating.validate({**FULL, "in_app_message": "x" * 501}, ALLOWED)

    def test_html_values_are_escaped_in_html_and_text_is_plain(self):
        context = {name: "x" for name in ALLOWED} | {"title": "<script>alert(1)</script> & co"}
        out = templating.render({"subject": "{{ title }}", "body_text": "{{ title }}", "body_html": "<p>{{ title }}</p>", "in_app_title": "{{ title }}", "in_app_message": "m"}, context)
        assert "<script>" not in out["body_html"] and "&lt;script&gt;" in out["body_html"] and "&amp; co" in out["body_html"]
        assert out["body_text"] == "<script>alert(1)</script> & co"  # text is not HTML; consumers escape it when displaying

    def test_a_subject_can_never_contain_a_newline(self):
        out = templating.render({"subject": "Hello {{ title }}"}, {"title": "a\r\nBcc: attacker@example.com"})
        assert "\n" not in out["subject"] and "\r" not in out["subject"]

    def test_values_cannot_inject_template_syntax(self):
        out = templating.render({"subject": "{{ title }}", "body_text": "{{ title }}"}, {"title": "{{ settings.SECRET_KEY }} {% load x %}"})
        assert out["subject"] == "{{ settings.SECRET_KEY }} {% load x %}"  # a value is data, never re-interpreted

    def test_rendering_gets_only_the_context_it_is_given(self):
        out = templating.render({"subject": "{{ title }}|{{ nothing_here }}"}, {"title": "T"})
        assert out["subject"] == "T|"


# ---------------------------------------------------------------------------------------------------------------------------------- filters
class TestFilters:
    EVENT = registry.EVENTS["finding.raised"]

    def test_clean_and_match(self):
        raw = [{"attribute": "severity_rank", "operator": "GREATER_THAN_OR_EQUAL", "value": 30}, {"attribute": "category", "operator": "EQUALS", "value": "safety"}]
        clean, problems = filters.clean_filters(self.EVENT, raw)
        assert problems == [] and clean[0]["value"] == "30"
        assert filters.matches(self.EVENT, clean, {"severity_rank": 40, "category": "safety"})
        assert not filters.matches(self.EVENT, clean, {"severity_rank": 20, "category": "safety"})
        assert not filters.matches(self.EVENT, clean, {"severity_rank": 40, "category": "quality"})

    def test_a_missing_fact_never_matches(self):
        clean, _ = filters.clean_filters(self.EVENT, [{"attribute": "category", "operator": "EQUALS", "value": "safety"}])
        assert not filters.matches(self.EVENT, clean, {})  # unknown -> not applied (fail closed)

    @pytest.mark.parametrize("raw", [[{"attribute": "password", "operator": "EQUALS", "value": "x"}], [{"attribute": "category", "operator": "EVAL", "value": "x"}],
                                     [{"attribute": "category", "operator": "GREATER_THAN", "value": "x"}], [{"attribute": "severity_rank", "operator": "EQUALS", "value": "abc"}],
                                     [{"attribute": "category", "operator": "EQUALS", "value": "x", "extra": 1}], "category = safety", [{}] * 6, [{"attribute": "category"}]])
    def test_malformed_filters_are_refused(self, raw):
        assert filters.clean_filters(self.EVENT, raw)[1]

    def test_no_filters_means_always(self):
        assert filters.matches(self.EVENT, [], {}) and filters.clean_filters(self.EVENT, None) == ([], [])

    def test_list_operands(self):
        ev = registry.EVENTS["evaluation.recorded"]
        clean, problems = filters.clean_filters(ev, [{"attribute": "outcome", "operator": "CONTAINS_ANY", "value": ["fail", "review_required"]}])
        assert problems == [] and filters.matches(ev, clean, {"outcome": "fail"}) and not filters.matches(ev, clean, {"outcome": "pass"})


# ---------------------------------------------------------------------------------------------------------------------------------- misc
class TestHelpers:
    @pytest.mark.parametrize("attempt,expected", [(1, 60), (2, 120), (3, 240), (6, 1920), (7, 3600), (20, 3600)])
    def test_bounded_exponential_backoff(self, attempt, expected):
        assert backoff_seconds(attempt) == expected

    def test_state_machine_has_no_way_out_of_terminal_states(self):
        assert k.TRANSITIONS[k.DeliveryStatus.DELIVERED] == () and k.TRANSITIONS[k.DeliveryStatus.CANCELLED] == () and k.TRANSITIONS[k.DeliveryStatus.SUPPRESSED] == ()
        assert k.DeliveryStatus.SENT not in k.TRANSITIONS[k.DeliveryStatus.PENDING]  # nothing is "sent" without a provider attempt
        assert k.TRANSITIONS[k.DeliveryStatus.SENT] == (k.DeliveryStatus.DELIVERED,)
        assert k.TRANSITIONS[k.DeliveryStatus.FAILED] == (k.DeliveryStatus.PENDING, k.DeliveryStatus.CANCELLED)

    def test_address_validation_and_masking(self):
        assert valid_address("alex@example.com") and not valid_address("") and not valid_address("not-an-email") and not valid_address("a@b.com\nBcc: c@d.com")
        assert not valid_address("a@b.com,c@d.com") and not valid_address("a@b.com;c@d.com")
        assert mask_email("alex@example.com") == "a***@example.com" and mask_email("") == ""

    def test_provider_error_carries_a_sanitized_category(self):
        err = EmailProviderError("raw secret text", category="timeout", transient=True, status_code=504)
        assert (err.category, err.transient, err.status_code) == ("timeout", True, 504)


class TestGraphProviderWithoutCredentials:
    def test_an_unconfigured_graph_provider_never_pretends_success(self, settings):
        settings.MICROSOFT_AUTH_CONFIGURED = False
        settings.NOTIFICATION_GRAPH_SENDER = ""
        from apps.notifications.providers.base import EmailMessage

        with pytest.raises(EmailProviderError) as info:
            Microsoft365EmailProvider().send(EmailMessage("s", "b", ["a@example.com"]))
        assert info.value.category == "not_configured" and not info.value.transient
