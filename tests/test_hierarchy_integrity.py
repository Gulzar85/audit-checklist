"""Mandatory architectural tests: the hierarchy can never become invalid.

Each rule is proved at three levels, because each is a separate line of defence:

* the **service** layer (friendly errors, what the UI and API call);
* the **model** (``full_clean``, what forms/admin/imports run);
* the **database** (raw ORM writes that skip validation entirely).
"""

from __future__ import annotations

import threading

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from apps.locations import services
from apps.locations.models import Location

pytestmark = pytest.mark.django_db


def _raw_parent_update(location, parent):
    """Set ``parent`` with no validation at all (straight to SQL)."""
    with transaction.atomic():
        Location.objects.filter(pk=location.pk).update(parent=parent)


# ---------------------------------------------------------------------------
# A -> A
# ---------------------------------------------------------------------------
class TestSelfParent:
    def test_service_rejects(self, org_a, make_location):
        a = make_location(org_a, "A")
        with pytest.raises(ValidationError) as exc:
            services.move_location(a, a)
        assert "parent" in exc.value.message_dict

    def test_model_validation_rejects(self, org_a, make_location):
        a = make_location(org_a, "A")
        a.parent = a
        with pytest.raises(ValidationError) as exc:
            a.full_clean()
        assert "parent" in exc.value.message_dict

    def test_database_rejects(self, org_a, make_location):
        a = make_location(org_a, "A")
        with pytest.raises(IntegrityError):
            _raw_parent_update(a, a)


# ---------------------------------------------------------------------------
# A -> B -> C -> A
# ---------------------------------------------------------------------------
@pytest.fixture
def abc(org_a, make_location):
    """C is a child of B, which is a child of A."""
    a = make_location(org_a, "A")
    b = make_location(org_a, "B", parent=a)
    c = make_location(org_a, "C", parent=b)
    return a, b, c


class TestCycles:
    def test_service_rejects_three_node_cycle(self, abc):
        a, _, c = abc
        with pytest.raises(ValidationError) as exc:
            services.move_location(a, c)
        assert "parent" in exc.value.message_dict
        a.refresh_from_db()
        assert a.parent_id is None

    def test_service_rejects_two_node_cycle(self, abc):
        a, b, _ = abc
        with pytest.raises(ValidationError):
            services.move_location(a, b)

    def test_model_validation_rejects(self, abc):
        a, _, c = abc
        a.parent = c
        with pytest.raises(ValidationError) as exc:
            a.full_clean()
        assert "parent" in exc.value.message_dict

    def test_database_rejects_a_cycle_written_behind_the_applications_back(self, abc):
        a, _, c = abc
        with pytest.raises(IntegrityError):
            _raw_parent_update(a, c)
        a.refresh_from_db()
        assert a.parent_id is None

    def test_database_rejects_cycle_even_via_bulk_update_objects(self, abc):
        a, b, _ = abc
        a.parent = b
        with pytest.raises(IntegrityError), transaction.atomic():
            Location.objects.bulk_update([a], ["parent"])

    def test_moving_to_a_non_descendant_is_allowed(self, abc, org_a, make_location):
        _, b, c = abc
        other = make_location(org_a, "Other")
        moved = services.move_location(c, other)
        assert moved.parent == other
        assert b.children.count() == 0

    def test_moving_to_root(self, abc):
        _, b, _ = abc
        assert services.move_location(b, None).parent is None

    def test_deep_chain_cycle_is_still_detected(self, org_a, make_location):
        nodes = [make_location(org_a, "n0")]
        for i in range(1, 30):
            nodes.append(make_location(org_a, f"n{i}", parent=nodes[-1]))
        with pytest.raises(ValidationError):
            services.move_location(nodes[0], nodes[-1])
        with pytest.raises(IntegrityError):
            _raw_parent_update(nodes[0], nodes[-1])


# ---------------------------------------------------------------------------
# Cross-organization parent
# ---------------------------------------------------------------------------
class TestCrossOrganizationParent:
    def test_service_rejects_on_create(self, org_a, org_b, make_location, make_type):
        foreign = make_location(org_b, "B1")
        with pytest.raises(ValidationError) as exc:
            services.create_location(
                organization=org_a, parent=foreign, location_type=make_type(org_a), name="A1", code="A1"
            )
        assert "parent" in exc.value.message_dict

    def test_service_rejects_on_move(self, org_a, org_b, make_location):
        a1 = make_location(org_a, "A1")
        b1 = make_location(org_b, "B1")
        with pytest.raises(ValidationError) as exc:
            services.move_location(a1, b1)
        assert "parent" in exc.value.message_dict
        a1.refresh_from_db()
        assert a1.parent_id is None

    def test_model_validation_rejects(self, org_a, org_b, make_location):
        a1 = make_location(org_a, "A1")
        b1 = make_location(org_b, "B1")
        a1.parent = b1
        with pytest.raises(ValidationError) as exc:
            a1.full_clean()
        assert "parent" in exc.value.message_dict

    def test_database_rejects_on_update(self, org_a, org_b, make_location):
        a1 = make_location(org_a, "A1")
        b1 = make_location(org_b, "B1")
        with pytest.raises(IntegrityError):
            _raw_parent_update(a1, b1)

    def test_database_rejects_on_insert(self, org_a, org_b, make_location, make_type):
        b1 = make_location(org_b, "B1")
        with pytest.raises(IntegrityError), transaction.atomic():
            Location.objects.create(
                organization=org_a, parent=b1, location_type=make_type(org_a), name="A1", code="A1"
            )


# ---------------------------------------------------------------------------
# Cross-organization location type
# ---------------------------------------------------------------------------
class TestCrossOrganizationType:
    def test_service_rejects(self, org_a, org_b, make_type):
        foreign_type = make_type(org_b, "Foreign", "FOREIGN")
        with pytest.raises(ValidationError) as exc:
            services.create_location(
                organization=org_a, location_type=foreign_type, name="A1", code="A1"
            )
        assert "location_type" in exc.value.message_dict

    def test_model_validation_rejects(self, org_a, org_b, make_location, make_type):
        a1 = make_location(org_a, "A1")
        a1.location_type = make_type(org_b, "Foreign", "FOREIGN")
        with pytest.raises(ValidationError) as exc:
            a1.full_clean()
        assert "location_type" in exc.value.message_dict

    def test_database_rejects_on_update(self, org_a, org_b, make_location, make_type):
        a1 = make_location(org_a, "A1")
        foreign_type = make_type(org_b, "Foreign", "FOREIGN")
        with pytest.raises(IntegrityError), transaction.atomic():
            Location.objects.filter(pk=a1.pk).update(location_type=foreign_type)

    def test_database_rejects_on_insert(self, org_a, org_b, make_type):
        foreign_type = make_type(org_b, "Foreign", "FOREIGN")
        with pytest.raises(IntegrityError), transaction.atomic():
            Location.objects.create(
                organization=org_a, location_type=foreign_type, name="A1", code="A1"
            )

    def test_platform_types_are_usable_by_every_organization(self, org_a, org_b, make_type):
        platform = make_type(None, "Site", "SITE")
        for org in (org_a, org_b):
            services.create_location(organization=org, location_type=platform, name="S", code="S")


# ---------------------------------------------------------------------------
# Organization is immutable once a location exists
# ---------------------------------------------------------------------------
class TestOrganizationImmutability:
    def test_service_rejects(self, org_a, org_b, make_location):
        a1 = make_location(org_a, "A1")
        with pytest.raises(ValidationError) as exc:
            services.update_location(a1, organization=org_b)
        assert "organization" in exc.value.message_dict

    def test_model_validation_rejects(self, org_a, org_b, make_location, make_type):
        a1 = make_location(org_a, "A1")
        a1.organization = org_b
        with pytest.raises(ValidationError) as exc:
            a1.full_clean()
        assert "organization" in exc.value.message_dict

    def test_database_rejects(self, org_a, org_b, make_location):
        a1 = make_location(org_a, "A1")
        with pytest.raises(IntegrityError), transaction.atomic():
            Location.objects.filter(pk=a1.pk).update(organization=org_b)


# ---------------------------------------------------------------------------
# Other database constraints
# ---------------------------------------------------------------------------
class TestConstraints:
    def test_duplicate_code_in_organization_rejected(self, org_a, make_location):
        make_location(org_a, "One", code="DUP")
        with pytest.raises(ValidationError) as exc:
            services.create_location(
                organization=org_a,
                location_type=Location.objects.get(code="DUP").location_type,
                name="Two",
                code="dup",
            )
        assert "code" in exc.value.message_dict

    def test_same_code_allowed_across_organizations(self, org_a, org_b, make_location):
        make_location(org_a, "One", code="SHARED")
        make_location(org_b, "One", code="SHARED")

    def test_database_rejects_duplicate_code(self, org_a, make_location):
        first = make_location(org_a, "One", code="DUP")
        with pytest.raises(IntegrityError), transaction.atomic():
            Location.objects.create(
                organization=org_a, location_type=first.location_type, name="Two", code="DUP"
            )

    def test_coordinates_must_be_a_pair(self, org_a, make_location):
        a1 = make_location(org_a, "A1")
        with pytest.raises(IntegrityError), transaction.atomic():
            Location.objects.filter(pk=a1.pk).update(latitude=10)

    def test_coordinate_ranges_are_validated(self, org_a, make_location):
        a1 = make_location(org_a, "A1")
        a1.latitude, a1.longitude = 91, 10
        with pytest.raises(ValidationError) as exc:
            a1.full_clean()
        assert "latitude" in exc.value.message_dict

    def test_locations_are_protected_from_cascade_deletion(self, org_a, make_location):
        from django.db.models import ProtectedError

        parent = make_location(org_a, "Parent")
        make_location(org_a, "Child", parent=parent)
        with pytest.raises(ProtectedError):
            parent.delete()

    def test_organizations_with_locations_cannot_be_deleted(self, org_a, make_location):
        from django.db.models import ProtectedError

        make_location(org_a, "Keep")
        with pytest.raises(ProtectedError):
            org_a.delete()

    def test_database_error_is_translated_not_leaked(self, org_a, make_location, monkeypatch):
        """If a race slips past validation the user still gets a field error."""
        a = make_location(org_a, "A")
        b = make_location(org_a, "B", parent=a)
        # Disable the Python-level descendant check to simulate losing a race.
        monkeypatch.setattr(Location, "_is_in_subtree", lambda self, candidate: False)
        with pytest.raises(ValidationError) as exc:
            services.move_location(a, b)
        assert "parent" in exc.value.message_dict


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_concurrent_opposite_moves_cannot_form_a_cycle(org_a, make_location):
    """Two admins move A under B and B under A at the same instant.

    Exactly one must win; the loser gets a validation error; no cycle remains.
    """
    a = make_location(org_a, "A")
    b = make_location(org_a, "B")
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def move(child, parent):
        try:
            barrier.wait(timeout=10)
            services.move_location(Location.objects.get(pk=child.pk), Location.objects.get(pk=parent.pk))
            outcomes.append("moved")
        except ValidationError:
            outcomes.append("rejected")
        finally:
            connection.close()

    threads = [threading.Thread(target=move, args=(a, b)), threading.Thread(target=move, args=(b, a))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["moved", "rejected"]
    a.refresh_from_db()
    b.refresh_from_db()
    assert not (a.parent_id == b.pk and b.parent_id == a.pk)
    assert (a.parent_id == b.pk) != (b.parent_id == a.pk)
