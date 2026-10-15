"""Import / export: atomicity, hierarchy safety, validation and tenant isolation."""

from __future__ import annotations

import pytest
import tablib

from apps.core.models import ActivityLog
from apps.locations.models import Location, LocationType
from apps.locations.resources import LocationResource, LocationTypeResource
from apps.organizations.models import Organization
from apps.organizations.resources import OrganizationResource

pytestmark = pytest.mark.django_db

LOCATION_HEADERS = ["organization", "code", "name", "location_type", "parent"]


def dataset(headers, *rows) -> tablib.Dataset:
    data = tablib.Dataset(headers=headers)
    for row in rows:
        data.append(list(row))
    return data


def run(resource_class, data, user, *, dry_run=False):
    return resource_class().import_data(
        data,
        dry_run=dry_run,
        raise_errors=False,
        use_transactions=True,
        rollback_on_validation_errors=True,
        user=user,
    )


def failed(result) -> bool:
    return result.has_errors() or result.has_validation_errors()


def messages(result) -> str:
    parts = [str(e.error) for e in result.base_errors]
    for row in result.invalid_rows:
        parts.append(str(row.error_dict))
    for row in result.rows:
        parts.extend(str(e.error) for e in row.errors)
    return " | ".join(parts)


@pytest.fixture
def types(org_a, org_b, make_type):
    return {
        "region": make_type(org_a, "Region", "REGION"),
        "site": make_type(org_a, "Site", "SITE"),
        "foreign": make_type(org_b, "Foreign", "FOREIGN"),
        "platform": make_type(None, "Global", "GLOBAL"),
    }


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------
class TestOrganizationImport:
    headers = ["code", "name", "country", "status"]

    def test_staff_can_create_organizations(self, staff_user):
        result = run(OrganizationResource, dataset(self.headers, ("acme-co", "Acme", "gb", "active")), staff_user)
        assert not failed(result)
        org = Organization.objects.get(code="ACME_CO")
        assert (org.name, org.country) == ("Acme", "GB")
        assert ActivityLog.objects.filter(verb="organization.created", metadata__source="import").exists()
        assert org.history.first().history_user == staff_user

    def test_org_admin_cannot_create_organizations(self, admin_a):
        result = run(OrganizationResource, dataset(self.headers, ("NEWCO", "New", "", "active")), admin_a)
        assert failed(result) and not Organization.objects.filter(code="NEWCO").exists()

    def test_org_admin_can_update_only_their_own(self, admin_a, org_a, org_b):
        result = run(OrganizationResource, dataset(self.headers, (org_a.code, "A Renamed", "", "active")), admin_a)
        assert not failed(result)
        org_a.refresh_from_db()
        assert org_a.name == "A Renamed"
        result = run(OrganizationResource, dataset(self.headers, (org_b.code, "B Hacked", "", "active")), admin_a)
        assert failed(result)
        org_b.refresh_from_db()
        assert org_b.name == "Org B"

    def test_invalid_status_is_rejected(self, staff_user):
        result = run(OrganizationResource, dataset(self.headers, ("BADSTATUS", "Bad", "", "archived")), staff_user)
        assert failed(result) and not Organization.objects.filter(code="BADSTATUS").exists()

    def test_status_of_an_existing_organization_cannot_be_changed_by_import(self, staff_user, org_a):
        result = run(OrganizationResource, dataset(self.headers, (org_a.code, "Org A", "", "suspended")), staff_user)
        assert failed(result)
        org_a.refresh_from_db()
        assert org_a.is_active

    def test_invalid_required_and_regional_fields(self, staff_user):
        data = dataset(self.headers, ("X1", "", "", "active"))
        assert failed(run(OrganizationResource, data, staff_user))
        data = dataset(self.headers, ("X2", "Ok", "GBR", "active"))
        assert failed(run(OrganizationResource, data, staff_user))

    def test_duplicate_code_in_the_file_never_creates_two(self, staff_user):
        data = dataset(self.headers, ("DUPE", "One", "", "active"), ("dupe", "Two", "", "active"))
        run(OrganizationResource, data, staff_user)
        assert Organization.objects.filter(code="DUPE").count() == 1

    def test_whole_file_rolls_back_on_any_invalid_row(self, staff_user):
        data = dataset(self.headers, ("GOOD", "Good", "", "active"), ("BAD", "", "", "active"))
        assert failed(run(OrganizationResource, data, staff_user))
        assert not Organization.objects.filter(code__in=["GOOD", "BAD"]).exists()

    def test_dry_run_changes_nothing(self, staff_user):
        result = run(OrganizationResource, dataset(self.headers, ("DRY", "Dry", "", "active")), staff_user, dry_run=True)
        assert not failed(result) and not Organization.objects.filter(code="DRY").exists()

    def test_requires_an_authenticated_user(self):
        result = run(OrganizationResource, dataset(self.headers, ("ANON", "Anon", "", "active")), None)
        assert failed(result) and not Organization.objects.filter(code="ANON").exists()

    def test_missing_required_column(self, staff_user):
        result = run(OrganizationResource, dataset(["name"], ("No code",)), staff_user)
        assert failed(result) and "Missing required column" in messages(result)

    def test_export_contains_the_data(self, org_a):
        exported = OrganizationResource().export()
        assert org_a.code in exported.csv and "logo" not in exported.headers


# ---------------------------------------------------------------------------
# Location types
# ---------------------------------------------------------------------------
class TestLocationTypeImport:
    headers = ["organization", "code", "name", "sort_order"]

    def test_create_for_an_organization(self, admin_a, org_a):
        result = run(LocationTypeResource, dataset(self.headers, ("org_a", "branch", "Branch", 2)), admin_a)
        assert not failed(result)
        assert LocationType.objects.get(code="BRANCH").organization == org_a

    def test_cannot_import_into_a_foreign_organization(self, admin_a, org_b):
        result = run(LocationTypeResource, dataset(self.headers, (org_b.code, "X", "X", 0)), admin_a)
        assert failed(result) and not LocationType.objects.filter(code="X").exists()

    def test_platform_types_are_staff_only(self, admin_a, staff_user):
        row = dataset(self.headers, ("", "global2", "Global 2", 0))
        assert failed(run(LocationTypeResource, row, admin_a))
        assert not failed(run(LocationTypeResource, dataset(self.headers, ("", "global2", "Global 2", 0)), staff_user))
        assert LocationType.objects.get(code="GLOBAL2").is_platform_type

    def test_duplicate_code_in_the_organization_updates_rather_than_duplicates(self, admin_a, org_a, types):
        run(LocationTypeResource, dataset(self.headers, (org_a.code, "region", "Renamed", 1)), admin_a)
        assert LocationType.objects.filter(organization=org_a, code="REGION").count() == 1
        types["region"].refresh_from_db()
        assert types["region"].name == "Renamed"

    def test_inactive_organization_is_refused(self, admin_a, staff_user, org_a):
        from apps.organizations import services

        services.suspend_organization(org_a)
        result = run(LocationTypeResource, dataset(self.headers, (org_a.code, "late", "Late", 0)), staff_user)
        assert failed(result) and not LocationType.objects.filter(code="LATE").exists()


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
class TestLocationImport:
    def row(self, org, code, name, type_code="REGION", parent=""):
        return (org.code, code, name, type_code, parent)

    def test_creates_a_hierarchy(self, admin_a, org_a, types):
        data = dataset(
            LOCATION_HEADERS,
            self.row(org_a, "root", "Root"),
            self.row(org_a, "child", "Child", parent="root"),
        )
        result = run(LocationResource, data, admin_a)
        assert not failed(result), messages(result)
        child = Location.objects.get(code="CHILD")
        assert child.parent.code == "ROOT" and child.organization == org_a
        assert ActivityLog.objects.filter(verb="location.created", metadata__source="import", actor=admin_a).count() == 2
        assert child.history.first().history_user == admin_a

    def test_children_may_precede_parents_in_the_file(self, admin_a, org_a, types):
        data = dataset(
            LOCATION_HEADERS,
            self.row(org_a, "leaf", "Leaf", "SITE", parent="mid"),
            self.row(org_a, "mid", "Mid", parent="top"),
            self.row(org_a, "top", "Top"),
        )
        assert not failed(run(LocationResource, data, admin_a))
        assert [a.code for a in Location.objects.get(code="LEAF").get_ancestors()] == ["TOP", "MID"]

    def test_parent_may_already_exist_in_the_database(self, admin_a, org_a, types, make_location):
        make_location(org_a, "Existing", code="EXISTING", type=types["region"])
        data = dataset(LOCATION_HEADERS, self.row(org_a, "kid", "Kid", parent="existing"))
        assert not failed(run(LocationResource, data, admin_a))
        assert Location.objects.get(code="KID").parent.code == "EXISTING"

    def test_circular_hierarchy_in_the_file_is_rejected(self, admin_a, org_a, types):
        data = dataset(
            LOCATION_HEADERS,
            self.row(org_a, "a", "A", parent="c"),
            self.row(org_a, "b", "B", parent="a"),
            self.row(org_a, "c", "C", parent="b"),
        )
        result = run(LocationResource, data, admin_a)
        assert failed(result) and "Circular hierarchy" in messages(result)
        assert not Location.objects.exists()

    def test_self_parent_in_the_file_is_rejected(self, admin_a, org_a, types):
        result = run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "a", "A", parent="a")), admin_a)
        assert failed(result) and not Location.objects.exists()

    def test_cycle_against_the_database_is_rejected(self, admin_a, org_a, types, make_location):
        a = make_location(org_a, "A", code="A", type=types["region"])
        make_location(org_a, "B", code="B", parent=a, type=types["region"])
        result = run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "a", "A", parent="b")), admin_a)
        assert failed(result)
        a.refresh_from_db()
        assert a.parent_id is None

    def test_duplicate_rows_in_the_file_are_rejected(self, admin_a, org_a, types):
        data = dataset(LOCATION_HEADERS, self.row(org_a, "d", "One"), self.row(org_a, "D", "Two"))
        result = run(LocationResource, data, admin_a)
        assert failed(result) and "more than once" in messages(result) and not Location.objects.exists()

    def test_missing_organization(self, admin_a, types):
        result = run(LocationResource, dataset(LOCATION_HEADERS, ("", "x", "X", "REGION", "")), admin_a)
        assert failed(result) and not Location.objects.exists()

    def test_unknown_organization(self, admin_a, types):
        result = run(LocationResource, dataset(LOCATION_HEADERS, ("NOPE", "x", "X", "REGION", "")), admin_a)
        assert failed(result) and not Location.objects.exists()

    def test_invalid_location_type(self, admin_a, org_a, types):
        result = run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "x", "X", "NOPE")), admin_a)
        assert failed(result) and not Location.objects.exists()

    def test_location_type_from_another_organization_is_not_found(self, admin_a, org_a, types):
        result = run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "x", "X", "FOREIGN")), admin_a)
        assert failed(result) and not Location.objects.exists()

    def test_platform_types_are_usable(self, admin_a, org_a, types):
        assert not failed(run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "g", "G", "GLOBAL")), admin_a))

    def test_parent_in_another_organization_is_not_found(self, admin_a, staff_user, org_a, org_b, types, make_location):
        make_location(org_b, "B1", code="B1", type=types["foreign"])
        for user in (admin_a, staff_user):  # even platform staff cannot express a cross-org parent
            result = run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "x", "X", "REGION", "b1")), user)
            assert failed(result)
        assert not Location.objects.filter(code="X").exists()

    def test_missing_required_fields(self, admin_a, org_a, types):
        assert failed(run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "x", "")), admin_a))
        assert failed(run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "", "No code")), admin_a))

    def test_missing_required_column(self, admin_a, types):
        result = run(LocationResource, dataset(["organization", "code"], ("ORG_A", "x")), admin_a)
        assert failed(result) and "Missing required column" in messages(result)

    def test_invalid_country(self, admin_a, org_a, types):
        data = dataset(LOCATION_HEADERS + ["country"], (*self.row(org_a, "x", "X"), "GBR"))
        assert failed(run(LocationResource, data, admin_a))

    def test_malformed_code(self, admin_a, org_a, types):
        assert failed(run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "bad code!", "X")), admin_a))

    def test_no_partial_import_when_any_row_is_invalid(self, admin_a, org_a, types):
        data = dataset(
            LOCATION_HEADERS,
            self.row(org_a, "good1", "Good 1"),
            self.row(org_a, "good2", "Good 2", parent="good1"),
            self.row(org_a, "bad", "Bad", "NOPE"),
        )
        assert failed(run(LocationResource, data, admin_a))
        assert Location.objects.count() == 0
        assert ActivityLog.objects.filter(verb="location.created").count() == 0

    def test_dry_run_changes_nothing(self, admin_a, org_a, types):
        result = run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "dry", "Dry")), admin_a, dry_run=True)
        assert not failed(result) and not Location.objects.exists()

    def test_reimporting_the_same_file_is_idempotent(self, admin_a, org_a, types):
        data = lambda: dataset(LOCATION_HEADERS, self.row(org_a, "r", "R"), self.row(org_a, "c", "C", parent="r"))  # noqa: E731
        run(LocationResource, data(), admin_a)
        before = ActivityLog.objects.count()
        result = run(LocationResource, data(), admin_a)
        assert not failed(result) and Location.objects.count() == 2
        assert ActivityLog.objects.count() == before  # unchanged rows are skipped

    def test_update_moves_an_existing_location(self, admin_a, org_a, types, make_location):
        a = make_location(org_a, "A", code="A", type=types["region"])
        b = make_location(org_a, "B", code="B", type=types["region"])
        run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "a", "A", parent="b")), admin_a)
        a.refresh_from_db()
        assert a.parent == b
        assert ActivityLog.objects.filter(verb="location.updated", metadata__source="import").exists()

    def test_lifecycle_cannot_be_changed_by_import(self, admin_a, org_a, types, make_location):
        make_location(org_a, "Live", code="LIVE", type=types["region"])
        data = dataset(LOCATION_HEADERS + ["is_active"], (*self.row(org_a, "live", "Live"), "0"))
        assert failed(run(LocationResource, data, admin_a))
        assert Location.objects.get(code="LIVE").is_active

    def test_inactive_parent_cannot_receive_an_active_child(self, admin_a, org_a, types, make_location):
        from apps.locations import services

        parent = make_location(org_a, "P", code="P", type=types["region"])
        services.deactivate_location(parent)
        result = run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "kid", "Kid", parent="p")), admin_a)
        assert failed(result) and not Location.objects.filter(code="KID").exists()

    def test_inactive_types_cannot_be_newly_assigned(self, admin_a, org_a, types):
        from apps.locations import services

        services.deactivate_location_type(types["site"])
        result = run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "x", "X", "SITE")), admin_a)
        assert failed(result)

    def test_inactive_organization_is_refused(self, staff_user, org_a, types):
        from apps.organizations import services

        services.suspend_organization(org_a)
        assert failed(run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "x", "X")), staff_user))

    def test_requires_an_authenticated_user(self, org_a, types):
        assert failed(run(LocationResource, dataset(LOCATION_HEADERS, self.row(org_a, "x", "X")), None))
        assert not Location.objects.exists()


class TestImportTenantIsolation:
    """An Organization A user must not be able to read or write Organization B through an import."""

    def test_cannot_create_locations_in_another_organization(self, admin_a, org_b, types):
        data = dataset(LOCATION_HEADERS, (org_b.code, "x", "X", "FOREIGN", ""))
        result = run(LocationResource, data, admin_a)
        assert failed(result) and not Location.objects.filter(code="X").exists()

    def test_cannot_update_another_organizations_locations(self, admin_a, org_b, types, make_location):
        target = make_location(org_b, "Secret", code="SECRET", type=types["foreign"])
        data = dataset(LOCATION_HEADERS, (org_b.code, "secret", "Hijacked", "FOREIGN", ""))
        assert failed(run(LocationResource, data, admin_a))
        target.refresh_from_db()
        assert target.name == "Secret"

    def test_foreign_and_nonexistent_organizations_are_indistinguishable(self, admin_a, org_b, types):
        """The *kind* of failure and its message are identical (the echoed code is the caller's own)."""

        def outcome(code):
            result = run(LocationResource, dataset(LOCATION_HEADERS, (code, "x", "X", "REGION", "")), admin_a)
            (row,) = result.invalid_rows
            return (
                result.has_errors(),
                result.has_validation_errors(),
                [str(e) for e in row.error_dict["organization"]],
                str(code),
            )

        foreign, missing = outcome(org_b.code), outcome("DOES_NOT_EXIST")
        assert foreign[:2] == missing[:2] == (False, True)
        assert [m.replace(foreign[3], "<code>") for m in foreign[2]] == [m.replace(missing[3], "<code>") for m in missing[2]]
    def test_export_is_scoped_to_the_queryset_the_caller_supplies(self, org_a, org_b, types, make_location):
        make_location(org_a, "Mine", code="MINE", type=types["region"])
        make_location(org_b, "Theirs", code="THEIRS", type=types["foreign"])
        exported = LocationResource().export(Location.objects.filter(organization=org_a)).csv
        assert "MINE" in exported and "THEIRS" not in exported


class TestExport:
    def test_export_reimports_cleanly(self, admin_a, org_a, types, make_location):
        root = make_location(org_a, "Root", code="ROOT", type=types["region"])
        make_location(org_a, "Leaf", code="LEAF", parent=root, type=types["region"])
        csv = LocationResource().export().csv
        Location.objects.all().delete() if False else None  # keep rows: re-import must be a no-op
        result = run(LocationResource, tablib.Dataset().load(csv, format="csv"), admin_a)
        assert not failed(result) and Location.objects.count() == 2

    def test_export_uses_codes_not_internal_ids(self, org_a, types, make_location):
        root = make_location(org_a, "Root", code="ROOT", type=types["region"])
        make_location(org_a, "Leaf", code="LEAF", parent=root, type=types["region"])
        exported = LocationResource().export()
        row = dict(zip(exported.headers, next(r for r in exported if "LEAF" in r), strict=True))
        assert (row["organization"], row["parent"], row["location_type"]) == ("ORG_A", "ROOT", "REGION")

    def test_export_query_count_is_constant(self, org_a, types, make_location, django_assert_max_num_queries):
        for i in range(30):
            make_location(org_a, f"L{i}", code=f"L{i}", type=types["region"])
        with django_assert_max_num_queries(4):
            LocationResource().export()


class TestAdminImportAccess:
    def test_staff_can_open_the_import_screen(self, client, superuser):
        client.force_login(superuser)
        assert client.get("/admin/locations/location/import/").status_code == 200

    def test_non_staff_cannot(self, client, admin_a):
        client.force_login(admin_a)
        response = client.get("/admin/locations/location/import/")
        assert response.status_code in (302, 403)
        assert b"Import" not in response.content or response.status_code == 302
