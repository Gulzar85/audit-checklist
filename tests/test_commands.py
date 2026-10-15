"""Management commands: validate_location_tree and seed_demo_data."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from apps.locations import services
from apps.locations.models import Location, LocationType
from apps.locations.selectors import find_integrity_problems
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db


def corrupt(sql: str, *params) -> None:
    """Run raw SQL with triggers suppressed, simulating data that bypassed every guard."""
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL session_replication_role = 'replica'")
        try:
            cursor.execute(sql, list(params))
        finally:
            cursor.execute("SET LOCAL session_replication_role = 'origin'")


def validate(*args) -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    call_command("validate_location_tree", *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


class TestIntegrityAudit:
    def test_a_healthy_tree_has_no_problems(self, chain_a):
        assert find_integrity_problems() == []
        out, _ = validate()
        assert "consistent" in out

    def test_empty_database_is_healthy(self):
        assert find_integrity_problems() == []

    def test_detects_a_cycle(self, org_a, make_location):
        a = make_location(org_a, "A")
        b = make_location(org_a, "B", parent=a)
        corrupt("UPDATE locations_location SET parent_id = %s WHERE id = %s", b.pk, a.pk)
        codes = {p.code for p in find_integrity_problems()}
        assert "cycle" in codes
        with pytest.raises(CommandError, match="integrity problem"):
            validate()

    def test_reports_each_node_on_the_cycle_but_not_nodes_that_merely_lead_into_it(self, org_a, make_location):
        a = make_location(org_a, "A")
        b = make_location(org_a, "B", parent=a)
        tail = make_location(org_a, "Tail", parent=b)
        corrupt("UPDATE locations_location SET parent_id = %s WHERE id = %s", b.pk, a.pk)
        flagged = {p.location_id for p in find_integrity_problems() if p.code == "cycle"}
        assert flagged == {a.pk, b.pk} and tail.pk not in flagged

    def test_detects_a_cross_organization_parent(self, org_a, org_b, make_location):
        a = make_location(org_a, "A")
        b = make_location(org_b, "B")
        corrupt("UPDATE locations_location SET parent_id = %s WHERE id = %s", b.pk, a.pk)
        assert "cross_organization_parent" in {p.code for p in find_integrity_problems()}

    def test_detects_a_cross_organization_type(self, org_a, org_b, make_location, make_type):
        a = make_location(org_a, "A")
        foreign = make_type(org_b, "F", "F")
        corrupt("UPDATE locations_location SET location_type_id = %s WHERE id = %s", foreign.pk, a.pk)
        assert "cross_organization_type" in {p.code for p in find_integrity_problems()}

    def test_detects_an_active_location_under_an_inactive_parent(self, org_a, make_location):
        parent = make_location(org_a, "P")
        make_location(org_a, "C", parent=parent)
        corrupt("UPDATE locations_location SET is_active = false WHERE id = %s", parent.pk)
        assert "active_under_inactive_parent" in {p.code for p in find_integrity_problems()}

    def test_can_be_limited_to_one_organization(self, org_a, org_b, make_location):
        a = make_location(org_a, "A")
        b = make_location(org_b, "B")
        corrupt("UPDATE locations_location SET parent_id = %s WHERE id = %s", b.pk, a.pk)
        assert find_integrity_problems(org_a)
        assert not find_integrity_problems(org_b)
        with pytest.raises(CommandError):
            validate("--organization", org_a.code)
        assert "consistent" in validate("--organization", org_b.code.lower())[0]

    def test_unknown_organization_is_a_clear_error(self):
        with pytest.raises(CommandError, match="No organization"):
            validate("--organization", "NOPE")

    def test_problems_are_described_for_a_human(self, org_a, make_location):
        a = make_location(org_a, "Alpha")
        b = make_location(org_a, "Beta", parent=a)
        corrupt("UPDATE locations_location SET parent_id = %s WHERE id = %s", b.pk, a.pk)
        messages = " ".join(p.message for p in find_integrity_problems())
        assert "Alpha" in messages and "circular" in messages

    def test_healthy_tree_stays_healthy_through_normal_operations(self, chain_a, org_a, make_location):
        elsewhere = make_location(org_a, "Elsewhere")
        services.move_location(chain_a[2], elsewhere)
        services.deactivate_location(chain_a[2], cascade=True)
        services.activate_location(chain_a[2])
        assert find_integrity_problems() == []


class TestSeedDemoData:
    def seed(self):
        out = io.StringIO()
        call_command("seed_demo_data", stdout=out)
        return out.getvalue()

    def test_creates_two_organizations_with_different_hierarchies(self):
        self.seed()
        demo, units = Organization.objects.get(code="DEMO"), Organization.objects.get(code="DEMO_BU")
        assert [lt.name for lt in LocationType.objects.filter(organization=demo)] == ["Global", "Region", "Area", "Location"]
        assert [lt.name for lt in LocationType.objects.filter(organization=units)] == [
            "Business Unit", "Division", "Facility", "Department",
        ]

    def test_builds_the_documented_hierarchy(self):
        self.seed()
        leaf = Location.objects.get(code="LOC-A")
        assert [a.name for a in leaf.get_ancestors()] == ["Global", "Region A", "Area A"]
        assert Location.objects.get(code="LOC-B").parent.name == "Area A"
        deep = Location.objects.get(code="DEP-2")
        assert [a.location_type.name for a in deep.get_ancestors()] == ["Business Unit", "Division", "Facility"]

    def test_is_idempotent(self):
        self.seed()
        counts = (Organization.objects.count(), LocationType.objects.count(), Location.objects.count())
        assert "already present" in self.seed()
        assert (Organization.objects.count(), LocationType.objects.count(), Location.objects.count()) == counts

    def test_the_result_passes_the_integrity_audit(self):
        self.seed()
        assert find_integrity_problems() == []

    def test_data_is_clearly_labelled_as_demo(self):
        self.seed()
        assert Organization.objects.get(code="DEMO").description.startswith("Demo data")
        assert Location.objects.get(code="LOC-A").metadata == {"demo": True}

    @pytest.mark.parametrize("environment", ["production", "PRODUCTION", "prod"])
    def test_refuses_to_run_in_production(self, settings, environment):
        settings.ENVIRONMENT = environment
        with pytest.raises(CommandError, match="production"):
            self.seed()
        assert not Organization.objects.exists()

    def test_creates_no_users(self, django_user_model):
        before = django_user_model.objects.count()
        self.seed()
        assert django_user_model.objects.count() == before

    def test_leaves_existing_organizations_alone(self, org_a, chain_a):
        before = Location.objects.filter(organization=org_a).count()
        self.seed()
        assert Location.objects.filter(organization=org_a).count() == before

    def test_it_is_logged_like_real_data(self):
        from apps.core.models import ActivityLog

        self.seed()
        assert ActivityLog.objects.filter(verb="location.created").count() == 13

@pytest.mark.django_db(transaction=True)
def test_audit_detects_a_self_parent_when_the_check_constraint_is_missing(org_a, make_location):
    """Self-parenting is blocked twice (CHECK constraint *and* trigger); prove the audit would still notice.

    Runs in a real transaction so the constraint can be dropped and triggers suppressed;
    both are *always* restored.
    """
    a = make_location(org_a, "A")
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE locations_location DROP CONSTRAINT location_cannot_be_its_own_parent")
        cursor.execute("SET session_replication_role = 'replica'")
        try:
            cursor.execute("UPDATE locations_location SET parent_id = id WHERE id = %s", [a.pk])
            found = {p.code for p in find_integrity_problems()}
        finally:
            cursor.execute("UPDATE locations_location SET parent_id = NULL WHERE id = %s", [a.pk])
            cursor.execute("SET session_replication_role = 'origin'")
            cursor.execute(
                "ALTER TABLE locations_location ADD CONSTRAINT location_cannot_be_its_own_parent "
                "CHECK (NOT (id = parent_id))"
            )
    assert "self_parent" in found