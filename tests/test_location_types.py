"""Location types: uniqueness, ownership, lifecycle and cross-organization rules."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.core.models import ActivityLog
from apps.locations import services
from apps.locations.models import LocationType
from apps.locations.selectors import available_location_types, location_types_visible_to

pytestmark = pytest.mark.django_db


class TestCreation:
    def test_organization_specific_type(self, org_a, admin_a):
        lt = services.create_location_type(organization=org_a, name="Region", code="region", actor=admin_a)
        assert lt.organization == org_a
        assert lt.code == "REGION"
        assert lt.is_active
        assert not lt.is_platform_type

    def test_platform_type_has_no_organization(self):
        lt = services.create_location_type(organization=None, name="Site", code="SITE")
        assert lt.is_platform_type

    def test_creation_is_logged_against_the_organization(self, org_a, admin_a):
        lt = services.create_location_type(organization=org_a, name="Region", code="REGION", actor=admin_a)
        entry = ActivityLog.objects.get(verb="location_type.created")
        assert (entry.organization, entry.actor, entry.target_id) == (org_a, admin_a, str(lt.pk))

    @pytest.mark.parametrize("code", ["has space!", "-leading", "", "lower_é"])
    def test_malformed_code_is_rejected(self, org_a, code):
        with pytest.raises(ValidationError) as exc:
            services.create_location_type(organization=org_a, name="X", code=code)
        assert "code" in exc.value.message_dict

    def test_inactive_organization_cannot_gain_types(self, org_a):
        org_a.status = "suspended"
        org_a.save()
        with pytest.raises(ValidationError) as exc:
            services.create_location_type(organization=org_a, name="X", code="X")
        assert "organization" in exc.value.message_dict


class TestUniqueness:
    def test_code_unique_within_organization(self, org_a):
        services.create_location_type(organization=org_a, name="One", code="DUP")
        with pytest.raises(ValidationError) as exc:
            services.create_location_type(organization=org_a, name="Two", code="dup")
        assert "code" in exc.value.message_dict

    def test_same_code_allowed_in_different_organizations(self, org_a, org_b):
        services.create_location_type(organization=org_a, name="Region", code="REGION")
        services.create_location_type(organization=org_b, name="Region", code="REGION")
        assert LocationType.objects.filter(code="REGION").count() == 2

    def test_platform_codes_are_unique_among_platform_types(self):
        services.create_location_type(organization=None, name="Site", code="SITE")
        with pytest.raises(ValidationError):
            services.create_location_type(organization=None, name="Site 2", code="SITE")

    def test_database_enforces_uniqueness_too(self, org_a, make_type):
        make_type(org_a, "One", "DUP")
        with pytest.raises(IntegrityError), transaction.atomic():
            LocationType.objects.create(organization=org_a, name="Two", code="DUP")


class TestUpdateAndLifecycle:
    def test_update_logs_changes(self, org_a, make_type, admin_a):
        lt = make_type(org_a, "Old", "OLD")
        services.update_location_type(lt, actor=admin_a, name="New", sort_order=5)
        entry = ActivityLog.objects.get(verb="location_type.updated")
        assert entry.metadata["changes"]["name"] == {"from": "Old", "to": "New"}

    def test_type_cannot_change_organization(self, org_a, org_b, make_type):
        lt = make_type(org_a, "Region", "REGION")
        with pytest.raises(ValidationError) as exc:
            services.update_location_type(lt, organization=org_b)
        assert "organization" in exc.value.message_dict

    def test_deactivate_and_reactivate(self, org_a, make_type, admin_a):
        lt = make_type(org_a, "Region", "REGION")
        services.deactivate_location_type(lt, actor=admin_a)
        lt.refresh_from_db()
        assert not lt.is_active
        services.activate_location_type(lt, actor=admin_a)
        lt.refresh_from_db()
        assert lt.is_active
        verbs = set(ActivityLog.objects.values_list("verb", flat=True))
        assert {"location_type.deactivated", "location_type.activated"} <= verbs

    def test_deactivated_type_keeps_existing_locations(self, org_a, make_type, make_location):
        lt = make_type(org_a, "Region", "REGION")
        loc = make_location(org_a, "North", type=lt)
        services.deactivate_location_type(lt)
        loc.refresh_from_db()
        assert loc.location_type == lt

    def test_deactivated_type_cannot_be_newly_assigned(self, org_a, make_type):
        lt = make_type(org_a, "Region", "REGION")
        services.deactivate_location_type(lt)
        with pytest.raises(ValidationError) as exc:
            services.create_location(organization=org_a, location_type=lt, name="X", code="X")
        assert "location_type" in exc.value.message_dict

    def test_history_is_recorded(self, org_a, make_type, admin_a):
        lt = make_type(org_a, "Old", "OLD")
        services.update_location_type(lt, actor=admin_a, name="New")
        assert lt.history.first().history_user == admin_a

    def test_types_are_protected_from_deletion_while_in_use(self, org_a, make_type, make_location):
        lt = make_type(org_a, "Region", "REGION")
        make_location(org_a, "North", type=lt)
        from django.db.models import ProtectedError

        with pytest.raises(ProtectedError):
            lt.delete()


class TestOwnership:
    def test_selector_returns_own_plus_platform_types_only(self, org_a, org_b, make_type):
        own = make_type(org_a, "Own", "OWN")
        platform = make_type(None, "Platform", "PLATFORM")
        other = make_type(org_b, "Other", "OTHER")
        available = set(available_location_types(org_a))
        assert available == {own, platform}
        assert other not in available

    def test_inactive_types_are_not_offered(self, org_a, make_type):
        gone = make_type(org_a, "Gone", "GONE", is_active=False)
        assert gone not in available_location_types(org_a)

    def test_visibility_is_scoped_to_the_users_organizations(self, org_a, org_b, admin_a, make_type):
        own = make_type(org_a, "Own", "OWN")
        platform = make_type(None, "Platform", "PLATFORM")
        other = make_type(org_b, "Other", "OTHER")
        assert set(location_types_visible_to(admin_a)) == {own, platform}
        assert other not in location_types_visible_to(admin_a)

    def test_anonymous_users_see_no_types(self, make_type, org_a):
        from django.contrib.auth.models import AnonymousUser

        make_type(None, "Platform", "PLATFORM")
        assert not location_types_visible_to(AnonymousUser()).exists()

    def test_rehoming_a_type_used_by_another_org_is_rejected_by_the_database(
        self, org_a, org_b, make_type, make_location
    ):
        shared = make_type(None, "Shared", "SHARED")
        make_location(org_a, "A node", type=shared)
        make_location(org_b, "B node", type=shared)
        with pytest.raises(IntegrityError), transaction.atomic():
            LocationType.objects.filter(pk=shared.pk).update(organization=org_a)

    def test_model_validation_also_rejects_rehoming(self, org_a, org_b, make_type, make_location):
        shared = make_type(None, "Shared", "SHARED")
        make_location(org_b, "B node", type=shared)
        shared.organization = org_a
        with pytest.raises(ValidationError) as exc:
            shared.full_clean()
        assert "organization" in exc.value.message_dict
