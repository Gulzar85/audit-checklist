"""Organization domain: identity, validation, lifecycle, history and logging."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.core.models import ActivityLog
from apps.organizations import services
from apps.organizations.models import Organization, normalize_organization_code
from apps.organizations.selectors import organizations_visible_to

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
class TestIdentity:
    def test_create_derives_slug_and_code_from_name(self):
        org = services.create_organization(name="Northwind Traders")
        assert org.slug == "northwind-traders"
        assert org.code == "NORTHWIND_TRADERS"

    def test_explicit_code_is_normalised(self):
        org = services.create_organization(name="Acme", code="acme-co")
        assert org.code == "ACME_CO"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("acme-co", "ACME_CO"), ("  Acme  Co ", "ACME_CO"), ("__x__", "X"), ("a/b\\c", "A_B_C")],
    )
    def test_normalize_code(self, raw, expected):
        assert normalize_organization_code(raw) == expected

    def test_duplicate_code_is_rejected_even_when_cased_differently(self):
        services.create_organization(name="First", code="ACME")
        with pytest.raises(ValidationError) as exc:
            services.create_organization(name="Second", code="acme")
        assert "code" in exc.value.message_dict

    def test_duplicate_slug_is_rejected(self):
        services.create_organization(name="First", slug="shared")
        with pytest.raises(ValidationError) as exc:
            services.create_organization(name="Second", slug="shared")
        assert "slug" in exc.value.message_dict

    def test_renaming_does_not_change_identity(self):
        org = services.create_organization(name="Old Name", code="STABLE")
        pk, code = org.pk, org.code
        services.update_organization(org, name="Completely New Name")
        org.refresh_from_db()
        assert (org.pk, org.code, org.name) == (pk, code, "Completely New Name")

    def test_code_cannot_change_after_creation(self):
        org = services.create_organization(name="Acme", code="ACME")
        with pytest.raises(ValidationError) as exc:
            services.update_organization(org, code="OTHER")
        assert "code" in exc.value.message_dict
        org.refresh_from_db()
        assert org.code == "ACME"

    def test_resubmitting_the_same_code_is_a_noop(self):
        org = services.create_organization(name="Acme", code="ACME")
        services.update_organization(org, code="acme", name="Acme 2")
        org.refresh_from_db()
        assert org.code == "ACME"

    def test_database_rejects_malformed_code(self):
        org = services.create_organization(name="Acme")
        with pytest.raises(IntegrityError), transaction.atomic():
            Organization.objects.filter(pk=org.pk).update(code="lower-case")

    def test_database_rejects_unknown_status(self):
        org = services.create_organization(name="Acme")
        with pytest.raises(IntegrityError), transaction.atomic():
            Organization.objects.filter(pk=org.pk).update(status="bogus")


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------
class TestValidation:
    def test_lowercase_country_and_currency_are_normalised_not_rejected(self):
        org = services.create_organization(name="Acme", country="gb", currency="gbp")
        assert (org.country, org.currency) == ("GB", "GBP")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("timezone", "Mars/Olympus_Mons"),
            ("country", "GBR"),
            ("currency", "US"),
            ("language", "not a language"),
            ("primary_color", "red"),
            ("date_format", "%Y"),
        ],
    )
    def test_invalid_regional_values_are_rejected(self, field, value):
        with pytest.raises(ValidationError) as exc:
            services.create_organization(name="Acme", **{field: value})
        assert field in exc.value.message_dict

    def test_valid_regional_settings_are_accepted(self):
        org = services.create_organization(
            name="Acme",
            timezone="Europe/London",
            currency="EUR",
            language="en-gb",
            date_format="d/m/Y",
            primary_color="#4f46e5",
        )
        assert org.primary_color == "#4F46E5"
        assert org.effective_timezone == "Europe/London"

    def test_effective_settings_fall_back_to_platform_defaults(self):
        org = services.create_organization(name="Acme")
        assert org.effective_timezone == "UTC"
        assert org.effective_currency == "USD"
        assert org.effective_date_format == "Y-m-d"

    def test_name_with_no_sluggable_characters_needs_a_slug(self):
        with pytest.raises(ValidationError):
            services.create_organization(name="!!!")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
class TestLifecycle:
    def test_new_organizations_are_active(self):
        org = services.create_organization(name="Acme", status="suspended")
        assert org.status == Organization.Status.ACTIVE
        assert org.is_active

    def test_status_is_the_only_lifecycle_field(self):
        assert not any(f.name == "is_active" for f in Organization._meta.get_fields())
        assert Organization(status="active").is_active
        assert not Organization(status="inactive").is_active
        assert not Organization(status="suspended").is_active

    def test_deactivate_and_activate(self, org_a):
        services.deactivate_organization(org_a)
        org_a.refresh_from_db()
        assert org_a.status == "inactive"
        services.activate_organization(org_a)
        org_a.refresh_from_db()
        assert org_a.is_active

    def test_suspend(self, org_a):
        services.suspend_organization(org_a)
        org_a.refresh_from_db()
        assert org_a.is_suspended

    def test_unknown_status_is_rejected(self, org_a):
        with pytest.raises(ValidationError):
            services.set_organization_status(org_a, "archived")

    def test_status_cannot_be_changed_through_update(self, org_a):
        with pytest.raises(ValueError):
            services.update_organization(org_a, status="inactive")

    def test_deactivating_never_deletes_data(self, org_a, make_location):
        location = make_location(org_a, "Keep me")
        services.deactivate_organization(org_a)
        assert type(location).objects.filter(pk=location.pk).exists()

    def test_inactive_organizations_are_hidden_from_members_but_not_staff(
        self, org_a, admin_a, staff_user
    ):
        assert org_a in organizations_visible_to(admin_a)
        services.suspend_organization(org_a)
        assert org_a not in organizations_visible_to(admin_a)
        assert org_a in organizations_visible_to(staff_user)

    def test_transition_to_same_status_is_a_noop(self, org_a):
        before = ActivityLog.objects.count()
        services.activate_organization(org_a)
        assert ActivityLog.objects.count() == before


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
class TestAuditTrail:
    def test_create_is_logged_with_actor_and_organization(self, staff_user):
        org = services.create_organization(name="Acme", actor=staff_user)
        entry = ActivityLog.objects.get(verb="organization.created")
        assert entry.actor == staff_user
        assert entry.organization == org
        assert (entry.target_type, entry.target_id) == ("organizations.organization", str(org.pk))

    def test_update_logs_what_changed(self, org_a, staff_user):
        services.update_organization(org_a, actor=staff_user, name="Renamed", phone="123")
        entry = ActivityLog.objects.get(verb="organization.updated")
        assert entry.metadata["changes"]["name"] == {"from": "Org A", "to": "Renamed"}
        assert entry.metadata["changes"]["phone"] == {"from": "", "to": "123"}

    def test_noop_update_logs_nothing(self, org_a):
        before = ActivityLog.objects.count()
        services.update_organization(org_a, name=org_a.name)
        assert ActivityLog.objects.count() == before

    @pytest.mark.parametrize(
        ("action", "verb", "new_status"),
        [
            (services.deactivate_organization, "organization.deactivated", "inactive"),
            (services.suspend_organization, "organization.suspended", "suspended"),
        ],
    )
    def test_status_changes_are_logged(self, org_a, staff_user, action, verb, new_status):
        action(org_a, actor=staff_user)
        entry = ActivityLog.objects.get(verb=verb)
        assert entry.metadata == {"from": "active", "to": new_status}
        assert entry.actor == staff_user

    def test_activation_is_logged(self, org_a, staff_user):
        services.deactivate_organization(org_a)
        services.activate_organization(org_a, actor=staff_user)
        assert ActivityLog.objects.filter(verb="organization.activated", actor=staff_user).exists()

    def test_history_records_who_and_what(self, org_a, staff_user):
        services.update_organization(org_a, actor=staff_user, name="Renamed")
        latest = org_a.history.first()
        assert latest.name == "Renamed"
        assert latest.history_user == staff_user
        assert latest.history_change_reason == "organization.updated"
        assert org_a.history.count() == 2  # creation + update

    def test_history_user_is_recorded_for_status_changes(self, org_a, staff_user):
        services.suspend_organization(org_a, actor=staff_user)
        assert org_a.history.first().history_user == staff_user
