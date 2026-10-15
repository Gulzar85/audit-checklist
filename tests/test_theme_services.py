"""Theme lifecycle, constraints, versioning, rollback, cloning, import/export and audit trail."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.core.models import ActivityLog
from apps.themes import selectors, services
from apps.themes.constants import EXPORT_FORMAT, ThemeScope, ThemeStatus
from apps.themes.models import Theme, ThemeActivation, UserAppearance

pytestmark = pytest.mark.django_db

LOW_CONTRAST = {"light": {"foreground": "#cccccc", "background": "#ffffff"}}


class TestCreate:
    def test_creates_a_draft_with_a_stable_code(self, org_a, theme_admin_a):
        theme = services.create_theme(actor=theme_admin_a, organization=org_a, name="Corporate Dark")
        assert theme.status == ThemeStatus.DRAFT
        assert theme.scope == ThemeScope.ORGANIZATION
        assert theme.code == "CORPORATE_DARK"
        assert theme.version == 1
        assert theme.created_by == theme_admin_a
        assert theme.published_at is None

    def test_system_theme_has_no_organization(self, db):
        theme = services.create_theme(name="Platform look")
        assert theme.organization is None
        assert theme.scope == ThemeScope.SYSTEM

    def test_code_is_not_the_display_name(self, org_a):
        theme = services.create_theme(organization=org_a, name="Brand", code="my-code")
        services.update_theme(theme, name="Renamed")
        theme.refresh_from_db()
        assert (theme.name, theme.code) == ("Renamed", "MY_CODE")

    def test_code_cannot_change(self, org_a):
        theme = services.create_theme(organization=org_a, name="Brand")
        with pytest.raises(ValidationError):
            services.update_theme(theme, code="OTHER")

    def test_derived_codes_are_made_unique(self, org_a):
        first = services.create_theme(organization=org_a, name="Brand")
        second = services.create_theme(organization=org_a, name="Brand")
        assert first.code == "BRAND"
        assert second.code == "BRAND_2"

    def test_explicit_duplicate_code_is_rejected(self, org_a):
        services.create_theme(organization=org_a, name="One", code="SAME")
        with pytest.raises(ValidationError):
            services.create_theme(organization=org_a, name="Two", code="SAME")

    def test_same_code_may_exist_in_different_organizations(self, org_a, org_b):
        services.create_theme(organization=org_a, name="One", code="SAME")
        services.create_theme(organization=org_b, name="Two", code="SAME")

    def test_system_codes_are_unique_too(self, db):
        services.create_theme(name="One", code="SAME")
        with pytest.raises(ValidationError):
            services.create_theme(name="Two", code="SAME")

    def test_from_preset_copies_overrides_and_default_appearance(self, org_a):
        theme = services.create_theme(organization=org_a, name="Dark", preset="dark")
        assert theme.default_appearance == "dark"
        assert theme.source_preset == "dark"
        corporate = services.create_theme(organization=org_a, name="Corp", preset="corporate")
        assert corporate.tokens["light"]["primary"] == "#0b3d91"

    def test_unknown_preset_is_rejected(self, org_a):
        with pytest.raises(ValidationError):
            services.create_theme(organization=org_a, name="X", preset="nope")

    def test_explicit_tokens_override_the_preset(self, org_a):
        theme = services.create_theme(
            organization=org_a, name="X", preset="corporate", tokens={"light": {"primary": "#112233"}}
        )
        assert theme.tokens["light"]["primary"] == "#112233"
        assert theme.tokens["light"]["primary_hover"] == "#082f6f"

    def test_invalid_tokens_are_rejected(self, org_a):
        with pytest.raises(ValidationError):
            services.create_theme(organization=org_a, name="X", tokens={"light": {"primary": "red"}})
        assert Theme.objects.count() == 0

    def test_inactive_organization_cannot_get_themes(self, org_a):
        org_a.status = "inactive"
        org_a.save()
        with pytest.raises(ValidationError):
            services.create_theme(organization=org_a, name="X")

    def test_blank_name_is_rejected(self, org_a):
        with pytest.raises(ValidationError):
            services.create_theme(organization=org_a, name="   ")

    def test_creation_is_logged_with_history(self, org_a, theme_admin_a):
        theme = services.create_theme(actor=theme_admin_a, organization=org_a, name="Brand")
        entry = ActivityLog.objects.get(verb="theme.created")
        assert (entry.actor, entry.organization, entry.target_id) == (theme_admin_a, org_a, str(theme.pk))
        assert theme.history.count() == 1


class TestConstraints:
    def test_scope_must_match_organization(self, org_a):
        with pytest.raises(IntegrityError), transaction.atomic():
            Theme.objects.bulk_create([Theme(name="x", code="X", scope="system", organization=org_a)])

    def test_status_is_validated_in_the_database(self, org_a):
        with pytest.raises(IntegrityError), transaction.atomic():
            Theme.objects.bulk_create([Theme(name="x", code="X", organization=org_a, status="bogus")])

    def test_active_and_archived_cannot_coexist_as_one_state(self, org_a):
        """There is no ``is_active`` flag to contradict ``status`` - and an active theme must be published."""
        with pytest.raises(IntegrityError), transaction.atomic():
            Theme.objects.bulk_create([Theme(name="x", code="X", organization=org_a, status="active")])

    def test_only_one_active_theme_per_organization(self, org_a, make_theme):
        make_theme(org_a, "One", status=ThemeStatus.ACTIVE)
        other = make_theme(org_a, "Two", status=ThemeStatus.PUBLISHED)
        with pytest.raises(IntegrityError), transaction.atomic():
            Theme.objects.filter(pk=other.pk).update(status="active", activated_at=other.published_at)

    def test_only_one_active_system_theme(self, make_theme):
        make_theme(None, "One", status=ThemeStatus.ACTIVE)
        other = make_theme(None, "Two", status=ThemeStatus.PUBLISHED)
        with pytest.raises(IntegrityError), transaction.atomic():
            Theme.objects.filter(pk=other.pk).update(status="active", activated_at=other.published_at)

    def test_code_format_is_enforced(self, org_a):
        with pytest.raises(IntegrityError), transaction.atomic():
            Theme.objects.bulk_create([Theme(name="x", code="bad code", organization=org_a)])

    def test_user_appearance_is_one_per_user(self, user):
        UserAppearance.objects.create(user=user, mode="dark")
        with pytest.raises(IntegrityError), transaction.atomic():
            UserAppearance.objects.create(user=user, mode="light")


class TestLifecycle:
    def test_draft_to_published_to_active(self, org_a, theme_admin_a):
        theme = services.create_theme(actor=theme_admin_a, organization=org_a, name="Brand")
        theme = services.publish_theme(theme, actor=theme_admin_a)
        assert theme.status == ThemeStatus.PUBLISHED and theme.published_at
        theme = services.activate_theme(theme, actor=theme_admin_a)
        assert theme.status == ThemeStatus.ACTIVE and theme.activated_at
        assert selectors.get_active_theme(org_a) == theme

    def test_a_draft_cannot_be_activated_directly(self, org_a):
        theme = services.create_theme(organization=org_a, name="Brand")
        with pytest.raises(ValidationError):
            services.activate_theme(theme)

    def test_publishing_twice_is_rejected(self, org_a, make_theme):
        theme = make_theme(org_a, status=ThemeStatus.PUBLISHED)
        with pytest.raises(ValidationError):
            services.publish_theme(theme)

    def test_activating_replaces_the_previous_theme(self, org_a, make_theme):
        first = make_theme(org_a, "First", status=ThemeStatus.ACTIVE)
        second = make_theme(org_a, "Second", status=ThemeStatus.PUBLISHED)
        services.activate_theme(second)
        first.refresh_from_db()
        assert first.status == ThemeStatus.PUBLISHED  # demoted, never deleted
        assert Theme.objects.filter(organization=org_a, status="active").count() == 1

    def test_activation_is_idempotent(self, org_a, make_theme):
        theme = make_theme(org_a, status=ThemeStatus.ACTIVE)
        services.activate_theme(theme)
        assert ThemeActivation.objects.filter(organization=org_a).count() == 1

    def test_activation_ledger_records_periods(self, org_a, make_theme):
        first = make_theme(org_a, "First", status=ThemeStatus.ACTIVE)
        second = make_theme(org_a, "Second", status=ThemeStatus.PUBLISHED)
        services.activate_theme(second)
        ledger = list(ThemeActivation.objects.filter(organization=org_a).order_by("activated_at"))
        assert [a.theme for a in ledger] == [first, second]
        assert ledger[0].deactivated_at is not None and ledger[1].deactivated_at is None

    def test_scopes_are_independent(self, org_a, org_b, make_theme):
        a = make_theme(org_a, "A", status=ThemeStatus.ACTIVE)
        b = make_theme(org_b, "B", status=ThemeStatus.ACTIVE)
        system = make_theme(None, "S", status=ThemeStatus.ACTIVE)
        assert {t.status for t in (a, b, system)} == {ThemeStatus.ACTIVE}
        assert selectors.get_active_theme(org_a) == a
        assert selectors.get_active_theme(None) == system

    def test_archive_draft_and_published_but_not_active(self, org_a, make_theme):
        draft = make_theme(org_a, "D")
        published = make_theme(org_a, "P", status=ThemeStatus.PUBLISHED)
        active = make_theme(org_a, "A", status=ThemeStatus.ACTIVE)
        assert services.archive_theme(draft).status == ThemeStatus.ARCHIVED
        assert services.archive_theme(published).status == ThemeStatus.ARCHIVED
        with pytest.raises(ValidationError):
            services.archive_theme(active)

    def test_archived_cannot_be_activated(self, org_a, make_theme):
        theme = make_theme(org_a, status=ThemeStatus.ARCHIVED)
        with pytest.raises(ValidationError):
            services.activate_theme(theme)

    def test_inactive_organization_cannot_activate(self, org_a, make_theme):
        theme = make_theme(org_a, status=ThemeStatus.PUBLISHED)
        org_a.status = "suspended"
        org_a.save()
        with pytest.raises(ValidationError):
            services.activate_theme(theme)


class TestImmutability:
    def test_only_drafts_are_editable(self, org_a, make_theme):
        for status in (ThemeStatus.PUBLISHED, ThemeStatus.ACTIVE, ThemeStatus.ARCHIVED):
            theme = make_theme(org_a, f"T-{status}", status=status)
            with pytest.raises(ValidationError):
                services.update_theme(theme, name="Changed")

    def test_update_records_token_level_changes(self, org_a, theme_admin_a):
        theme = services.create_theme(actor=theme_admin_a, organization=org_a, name="Brand")
        services.update_theme(theme, actor=theme_admin_a, tokens={"light": {"primary": "#112233"}})
        services.update_theme(theme, actor=theme_admin_a, tokens={"light": {"primary": "#445566"}})
        history = selectors.theme_history(theme)
        assert history[0]["user"] == theme_admin_a
        assert history[0]["changes"] == [
            {"token": "primary", "label": "Primary (light)", "section": "light", "from": "#112233", "to": "#445566"}
        ]  # "who changed the primary colour, what was it before, what is it now"
        entry = ActivityLog.objects.filter(verb="theme.updated").first()
        assert entry.metadata["changes"]["tokens"][0]["to"] == "#445566"

    def test_a_noop_update_writes_nothing(self, org_a):
        theme = services.create_theme(organization=org_a, name="Brand")
        before = (ActivityLog.objects.count(), theme.history.count())
        services.update_theme(theme, name="Brand")
        assert (ActivityLog.objects.count(), theme.history.count()) == before


class TestAccessibilityGate:
    def test_publish_is_blocked_by_mandatory_contrast_failures(self, org_a):
        theme = services.create_theme(organization=org_a, name="Pale", tokens=LOW_CONTRAST)
        with pytest.raises(ValidationError) as caught:
            services.publish_theme(theme)
        assert "Text on page background" in " ".join(caught.value.message_dict["tokens"])
        theme.refresh_from_db()
        assert theme.status == ThemeStatus.DRAFT

    def test_advisory_warnings_do_not_block(self, org_a):
        theme = services.create_theme(organization=org_a, name="Faint links", tokens={"light": {"link": "#cccccc"}})
        assert services.publish_theme(theme).status == ThemeStatus.PUBLISHED

    def test_organization_themes_are_vetted_too(self, org_a):
        theme = services.create_theme(organization=org_a, name="Org", tokens={"light": {"foreground": "#cccccc"}})
        with pytest.raises(ValidationError):
            services.publish_theme(theme)


class TestVersions:
    def test_new_version_is_a_draft_copy_with_the_same_code(self, org_a, make_theme):
        v1 = make_theme(org_a, "Brand", status=ThemeStatus.ACTIVE, tokens={"light": {"primary": "#112233"}})
        v2 = services.create_new_version(v1)
        assert (v2.code, v2.version, v2.status) == (v1.code, 2, ThemeStatus.DRAFT)
        assert v2.tokens == v1.tokens and v2.pk != v1.pk
        v1.refresh_from_db()
        assert v1.status == ThemeStatus.ACTIVE  # untouched

    def test_only_one_draft_version_at_a_time(self, org_a, make_theme):
        v1 = make_theme(org_a, status=ThemeStatus.PUBLISHED)
        services.create_new_version(v1)
        with pytest.raises(ValidationError):
            services.create_new_version(v1)

    def test_versions_increment(self, org_a, make_theme):
        v1 = make_theme(org_a, status=ThemeStatus.PUBLISHED)
        v2 = services.create_new_version(v1)
        services.publish_theme(v2)
        v3 = services.create_new_version(v2)
        assert v3.version == 3

    def test_new_version_can_be_edited_published_and_activated(self, org_a, make_theme):
        v1 = make_theme(org_a, status=ThemeStatus.ACTIVE)
        v2 = services.create_new_version(v1)
        v2 = services.update_theme(v2, tokens={"light": {"primary": "#445566"}})
        services.publish_theme(v2)
        services.activate_theme(v2)
        assert selectors.get_active_theme(org_a) == v2
        assert Theme.objects.get(pk=v1.pk).status == ThemeStatus.PUBLISHED


class TestRollback:
    def test_rollback_restores_the_previous_theme(self, org_a, make_theme, theme_admin_a):
        first = make_theme(org_a, "First", status=ThemeStatus.ACTIVE)
        second = make_theme(org_a, "Second", status=ThemeStatus.PUBLISHED)
        second = services.activate_theme(second, actor=theme_admin_a)
        restored = services.rollback_theme(second, actor=theme_admin_a)
        assert restored == first
        assert selectors.get_active_theme(org_a) == first
        assert Theme.objects.get(pk=second.pk).status == ThemeStatus.PUBLISHED  # still available
        assert ActivityLog.objects.filter(verb="theme.rolled_back", actor=theme_admin_a).exists()
        assert ThemeActivation.objects.filter(organization=org_a, via="rollback").count() == 1

    def test_nothing_to_roll_back_to(self, org_a, make_theme):
        only = make_theme(org_a, status=ThemeStatus.ACTIVE)
        with pytest.raises(ValidationError):
            services.rollback_theme(only)

    def test_only_the_active_theme_can_be_rolled_back(self, org_a, make_theme):
        published = make_theme(org_a, status=ThemeStatus.PUBLISHED)
        with pytest.raises(ValidationError):
            services.rollback_theme(published)

    def test_archived_themes_are_not_restored(self, org_a, make_theme):
        first = make_theme(org_a, "First", status=ThemeStatus.ACTIVE)
        second = make_theme(org_a, "Second", status=ThemeStatus.PUBLISHED)
        second = services.activate_theme(second)
        services.archive_theme(Theme.objects.get(pk=first.pk))
        with pytest.raises(ValidationError):
            services.rollback_theme(second)

    def test_system_scope_rollback(self, make_theme):
        first = make_theme(None, "First", status=ThemeStatus.ACTIVE)
        second = services.activate_theme(make_theme(None, "Second", status=ThemeStatus.PUBLISHED))
        assert services.rollback_theme(second) == first


class TestClone:
    def test_clone_is_a_new_draft_with_its_own_code(self, org_a, make_theme):
        original = make_theme(org_a, "Corporate", status=ThemeStatus.ACTIVE, tokens={"light": {"primary": "#112233"}})
        clone = services.clone_theme(original, name="Corporate Dark")
        assert clone.pk != original.pk
        assert (clone.code, clone.status, clone.version) == ("CORPORATE_DARK", ThemeStatus.DRAFT, 1)
        assert clone.tokens == original.tokens
        original.refresh_from_db()
        assert original.status == ThemeStatus.ACTIVE and original.code == "CORPORATE"

    def test_clone_tokens_are_independent(self, org_a, make_theme):
        original = make_theme(org_a, "A", tokens={"light": {"primary": "#112233"}})
        clone = services.clone_theme(original)
        services.update_theme(clone, tokens={"light": {"primary": "#445566"}})
        original.refresh_from_db()
        assert original.tokens["light"]["primary"] == "#112233"

    def test_clone_a_system_theme_into_an_organization(self, org_a, make_theme):
        system = make_theme(None, "Platform", status=ThemeStatus.PUBLISHED)
        clone = services.clone_theme(system, organization=org_a)
        assert clone.organization == org_a and clone.scope == ThemeScope.ORGANIZATION

    def test_clone_is_logged(self, org_a, make_theme):
        original = make_theme(org_a, "A")
        clone = services.clone_theme(original)
        entry = ActivityLog.objects.get(verb="theme.cloned")
        assert entry.metadata["source_code"] == original.code and entry.target_id == str(clone.pk)


class TestExportImport:
    def test_export_contains_only_validated_configuration(self, org_a, make_theme, theme_admin_a):
        theme = make_theme(org_a, "Brand", tokens={"light": {"primary": "#112233"}}, actor=theme_admin_a)
        document = services.export_theme(theme)
        assert document["format"] == EXPORT_FORMAT
        assert set(document) == {"format", "name", "code", "description", "default_appearance", "source_preset", "tokens"}
        flattened = str(document)
        for secret in (str(theme.pk), str(org_a.pk), theme_admin_a.email, "password", "token="):
            assert secret not in flattened

    def test_roundtrip_creates_an_equal_draft(self, org_a, org_b, make_theme):
        theme = make_theme(org_a, "Brand", tokens={"light": {"primary": "#112233"}, "shared": {"radius_md": 10}})
        imported = services.import_theme(services.export_theme(theme), organization=org_b)
        assert imported.organization == org_b and imported.status == ThemeStatus.DRAFT
        assert imported.tokens == theme.tokens and imported.name == "Brand"

    @pytest.mark.parametrize(
        "document",
        [
            "not a dict",
            {},
            {"format": "other"},
            {"format": EXPORT_FORMAT, "name": "x", "tokens": {"light": {"primary": "javascript:alert(1)"}}},
            {"format": EXPORT_FORMAT, "name": "x", "tokens": {"custom_css": "body{}"}},
            {"format": EXPORT_FORMAT, "name": "x", "custom_css": "body{}"},
            {"format": EXPORT_FORMAT, "name": "", "tokens": {}},
            {"format": EXPORT_FORMAT, "name": "x", "default_appearance": "neon"},
            {"format": EXPORT_FORMAT, "name": "<script>alert(1)</script>", "tokens": {"light": {"primary": "red"}}},
        ],
    )
    def test_hostile_or_malformed_documents_are_rejected(self, org_a, document):
        with pytest.raises(ValidationError):
            services.import_theme(document, organization=org_a)
        assert Theme.objects.count() == 0

    def test_file_parsing_is_size_capped_and_strict(self):
        with pytest.raises(ValidationError):
            services.parse_import_file(b"x" * (70 * 1024))
        with pytest.raises(ValidationError):
            services.parse_import_file(b"\xff\xfe")
        with pytest.raises(ValidationError):
            services.parse_import_file(b"{not json")
        assert services.parse_import_file(b'{"a": 1}') == {"a": 1}


class TestUserAppearance:
    def test_set_change_and_clear(self, user):
        assert services.set_user_appearance(user, "dark") == "dark"
        assert services.set_user_appearance(user, "light") == "light"
        assert UserAppearance.objects.get(user=user).mode == "light"
        assert services.set_user_appearance(user, None) is None
        assert not UserAppearance.objects.filter(user=user).exists()

    def test_invalid_mode(self, user):
        with pytest.raises(ValidationError):
            services.set_user_appearance(user, "neon")

    def test_changes_are_audited_with_from_and_to(self, user, membership):
        services.set_user_appearance(user, "dark")
        services.set_user_appearance(user, "light")
        entries = list(ActivityLog.objects.filter(verb="appearance.preference_changed").order_by("created_at"))
        assert [e.metadata for e in entries] == [{"from": None, "to": "dark"}, {"from": "dark", "to": "light"}]
        assert entries[0].actor == user and entries[0].organization == membership.organization

    def test_unchanged_preference_is_not_logged(self, user):
        services.set_user_appearance(user, "dark")
        services.set_user_appearance(user, "dark")
        assert ActivityLog.objects.filter(verb="appearance.preference_changed").count() == 1
