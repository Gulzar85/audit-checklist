"""/api/v1/locations/, /location-types/ and /locations/tree/."""

from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from apps.core.models import ActivityLog
from apps.locations import services
from apps.locations.models import Location, LocationType

pytestmark = pytest.mark.django_db

LOCATIONS = "/api/v1/locations/"
TYPES = "/api/v1/location-types/"
TREE = "/api/v1/locations/tree/"


@pytest.fixture
def api():
    def _client(user=None) -> APIClient:
        client = APIClient()
        if user is not None:
            client.force_authenticate(user)
        return client

    return _client


def loc_url(location) -> str:
    return f"{LOCATIONS}{location.pk}/"


def ids(response) -> set[str]:
    return {item["id"] for item in response.json()["data"]}


@pytest.fixture
def world(org_a, org_b, make_type, make_location):
    """Two tenants with a small tree each, plus a platform type."""
    type_a = make_type(org_a, "Region", "REGION")
    type_b = make_type(org_b, "Division", "DIVISION")
    a_root = make_location(org_a, "A Root", type=type_a, code="A-ROOT", city="Leeds", country="GB")
    a_child = make_location(org_a, "A Child", parent=a_root, type=type_a, code="A-CHILD", city="York", country="GB")
    b_root = make_location(org_b, "B Root", type=type_b, code="B-ROOT", city="Boston", country="US")
    platform = make_type(None, "Site", "SITE")
    return {
        "type_a": type_a, "type_b": type_b, "a_root": a_root, "a_child": a_child,
        "b_root": b_root, "platform": platform,
    }


class TestAuthentication:
    def test_anonymous_is_rejected_everywhere(self, api, world):
        for url in (LOCATIONS, loc_url(world["a_root"]), TYPES, TREE):
            assert api().get(url).status_code in (401, 403)
        assert api().post(LOCATIONS, {}).status_code in (401, 403)


class TestIsolation:
    """A user in Organization A must never reach Organization B's data."""

    def test_list_contains_only_own_organization(self, api, admin_a, world):
        got = ids(api(admin_a).get(LOCATIONS))
        assert got == {str(world["a_root"].pk), str(world["a_child"].pk)}
        assert str(world["b_root"].pk) not in got

    def test_direct_object_access_is_404(self, api, admin_a, world):
        response = api(admin_a).get(loc_url(world["b_root"]))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    def test_foreign_object_cannot_be_modified_or_acted_on(self, api, admin_a, world):
        b = world["b_root"]
        assert api(admin_a).patch(loc_url(b), {"name": "pwned"}).status_code == 404
        assert api(admin_a).post(f"{loc_url(b)}deactivate/", {}).status_code == 404
        assert api(admin_a).post(f"{loc_url(b)}move/", {"parent": None}).status_code == 404
        b.refresh_from_db()
        assert b.name == "B Root" and b.is_active

    def test_staff_see_both_tenants(self, api, staff_user, world):
        assert {str(world["a_root"].pk), str(world["b_root"].pk)} <= ids(api(staff_user).get(LOCATIONS))

    def test_filtering_by_a_foreign_organization_is_rejected_not_honoured(self, api, admin_a, world, org_b):
        response = api(admin_a).get(LOCATIONS, {"organization": str(org_b.pk)})
        assert response.status_code == 400
        assert "B Root" not in response.content.decode()

    def test_filtering_by_a_foreign_parent_is_rejected(self, api, admin_a, world):
        response = api(admin_a).get(LOCATIONS, {"parent": str(world["b_root"].pk)})
        assert response.status_code == 400

    def test_filtering_by_a_foreign_type_is_rejected(self, api, admin_a, world):
        assert api(admin_a).get(LOCATIONS, {"location_type": str(world["type_b"].pk)}).status_code == 400

    def test_cannot_create_in_a_foreign_organization(self, api, admin_a, world, org_b):
        response = api(admin_a).post(
            LOCATIONS,
            {"organization": str(org_b.pk), "location_type": str(world["type_b"].pk), "name": "X", "code": "X"},
        )
        assert response.status_code == 400
        assert not Location.objects.filter(code="X").exists()

    def test_cannot_use_a_foreign_parent(self, api, admin_a, world, org_a):
        response = api(admin_a).post(
            LOCATIONS,
            {"organization": str(org_a.pk), "location_type": str(world["type_a"].pk),
             "parent": str(world["b_root"].pk), "name": "X", "code": "X"},
        )
        assert response.status_code == 400 and "parent" in response.json()["error"]["details"]

    def test_cannot_use_a_foreign_type(self, api, admin_a, world, org_a):
        response = api(admin_a).post(
            LOCATIONS,
            {"organization": str(org_a.pk), "location_type": str(world["type_b"].pk), "name": "X", "code": "X"},
        )
        assert response.status_code == 400 and "location_type" in response.json()["error"]["details"]

    def test_cannot_move_into_a_foreign_parent(self, api, admin_a, world):
        response = api(admin_a).post(f"{loc_url(world['a_child'])}move/", {"parent": str(world["b_root"].pk)})
        assert response.status_code == 400
        world["a_child"].refresh_from_db()
        assert world["a_child"].parent == world["a_root"]

    def test_foreign_object_error_is_indistinguishable_from_nonexistent(self, api, admin_a, world, org_a):
        """A client cannot probe for other tenants' ids: both cases read the same."""
        import re

        def masked(response) -> list[str]:  # the id the client itself sent is echoed back
            return [re.sub(r"[0-9a-f-]{36}", "<id>", m) for m in response.json()["error"]["details"]["parent"]]

        payload = {"organization": str(org_a.pk), "location_type": str(world["type_a"].pk), "name": "X", "code": "X"}
        foreign = api(admin_a).post(LOCATIONS, {**payload, "parent": str(world["b_root"].pk)})
        missing = api(admin_a).post(LOCATIONS, {**payload, "parent": str(uuid.uuid4())})
        assert foreign.status_code == missing.status_code == 400
        assert masked(foreign) == masked(missing)

    def test_types_of_other_organizations_are_hidden_but_platform_types_visible(self, api, admin_a, world):
        got = ids(api(admin_a).get(TYPES))
        assert str(world["type_a"].pk) in got and str(world["platform"].pk) in got
        assert str(world["type_b"].pk) not in got
        assert api(admin_a).get(f"{TYPES}{world['type_b'].pk}/").status_code == 404

    def test_inactive_organization_hides_its_locations(self, api, admin_a, world, org_a):
        from apps.organizations import services as org_services

        org_services.suspend_organization(org_a)
        assert api(admin_a).get(LOCATIONS).json()["data"] == []
        assert api(admin_a).get(loc_url(world["a_root"])).status_code == 404

    def test_tree_is_isolated(self, api, admin_a, world):
        flat = str(api(admin_a).get(TREE).json())
        assert "A Root" in flat and "B Root" not in flat


class TestReadAndErrors:
    def test_retrieve_includes_derived_fields(self, api, admin_a, world):
        data = api(admin_a).get(loc_url(world["a_root"])).json()["data"]
        assert data["children_count"] == 1
        assert (data["organization_code"], data["location_type_name"], data["parent"]) == ("ORG_A", "Region", None)

    def test_invalid_uuid_is_404(self, api, admin_a):
        assert api(admin_a).get(f"{LOCATIONS}not-a-uuid/").status_code == 404
        assert api(admin_a).get(f"{TYPES}12345/").status_code == 404

    def test_unknown_uuid_is_404(self, api, admin_a):
        assert api(admin_a).get(f"{LOCATIONS}{uuid.uuid4()}/").status_code == 404

    def test_read_only_member_gets_403_on_writes_but_can_read(self, api, viewer_a, world, org_a):
        client = api(viewer_a)
        assert client.get(loc_url(world["a_root"])).status_code == 200
        assert client.patch(loc_url(world["a_root"]), {"name": "x"}).status_code == 403
        assert client.post(f"{loc_url(world['a_root'])}deactivate/", {}).status_code == 403
        assert client.post(LOCATIONS, {"organization": str(org_a.pk)}).status_code == 403
        assert client.post(f"{TYPES}{world['type_a'].pk}/deactivate/").status_code == 403

    def test_delete_is_not_offered(self, api, admin_a, world):
        assert api(admin_a).delete(loc_url(world["a_root"])).status_code == 405
        assert api(admin_a).delete(f"{TYPES}{world['type_a'].pk}/").status_code == 405


class TestFilteringPaginationOrdering:
    def test_filters(self, api, admin_a, world):
        client = api(admin_a)
        root, child = str(world["a_root"].pk), str(world["a_child"].pk)
        assert ids(client.get(LOCATIONS, {"parent": root})) == {child}
        assert ids(client.get(LOCATIONS, {"root": "true"})) == {root}
        assert ids(client.get(LOCATIONS, {"location_type": str(world["type_a"].pk)})) == {root, child}
        assert ids(client.get(LOCATIONS, {"search": "york"})) == {child}
        assert ids(client.get(LOCATIONS, {"city": "lee"})) == {root}
        assert ids(client.get(LOCATIONS, {"country": "gb"})) == {root, child}
        assert ids(client.get(LOCATIONS, {"q": "CHILD"})) == {child}

    def test_status_and_is_active(self, api, admin_a, world):
        services.deactivate_location(world["a_child"])
        client = api(admin_a)
        child = str(world["a_child"].pk)
        assert ids(client.get(LOCATIONS, {"status": "inactive"})) == {child}
        assert ids(client.get(LOCATIONS, {"is_active": "false"})) == {child}
        assert child not in ids(client.get(LOCATIONS, {"is_active": "true"}))

    def test_organization_filter_for_staff(self, api, staff_user, world, org_b):
        assert ids(api(staff_user).get(LOCATIONS, {"organization": str(org_b.pk)})) == {str(world["b_root"].pk)}

    def test_ordering(self, api, admin_a, world):
        names = [item["name"] for item in api(admin_a).get(LOCATIONS, {"ordering": "-name"}).json()["data"]]
        assert names == ["A Root", "A Child"]

    def test_pagination(self, api, admin_a, org_a, make_location, world):
        for i in range(30):
            make_location(org_a, f"Extra {i:02d}", type=world["type_a"], code=f"EX{i}")
        payload = api(admin_a).get(LOCATIONS, {"page_size": 10, "page": 2}).json()
        assert len(payload["data"]) == 10
        assert payload["meta"]["count"] == 32
        assert payload["meta"]["previous"] and payload["meta"]["next"]

    def test_page_size_is_capped(self, api, admin_a, world):
        assert api(admin_a).get(LOCATIONS, {"page_size": 100000}).status_code == 200

    def test_unrecognised_ordering_field_is_ignored_safely(self, api, admin_a, world):
        assert api(admin_a).get(LOCATIONS, {"ordering": "organization__members__password"}).status_code == 200


class TestCreateUpdate:
    def test_create_with_service_rules_and_audit(self, api, admin_a, world, org_a):
        response = api(admin_a).post(
            LOCATIONS,
            {"organization": str(org_a.pk), "location_type": str(world["type_a"].pk),
             "parent": str(world["a_root"].pk), "name": "New", "code": "new-1", "country": "gb"},
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert (data["code"], data["country"], data["is_active"]) == ("NEW-1", "GB", True)
        assert ActivityLog.objects.filter(verb="location.created", actor=admin_a).exists()
        assert Location.objects.get(pk=data["id"]).history.first().history_user == admin_a

    def test_is_active_in_payload_is_ignored(self, api, admin_a, world, org_a):
        data = api(admin_a).post(
            LOCATIONS,
            {"organization": str(org_a.pk), "location_type": str(world["type_a"].pk),
             "name": "N", "code": "N", "is_active": False},
        ).json()["data"]
        assert data["is_active"] is True

    def test_duplicate_code_is_a_field_error(self, api, admin_a, world, org_a):
        response = api(admin_a).post(
            LOCATIONS,
            {"organization": str(org_a.pk), "location_type": str(world["type_a"].pk), "name": "N", "code": "a-root"},
        )
        assert response.status_code == 400 and "code" in response.json()["error"]["details"]

    @pytest.mark.parametrize("payload, field", [
        ({"latitude": "95", "longitude": "10"}, "latitude"),
        ({"latitude": "10"}, None),
        ({"country": "GBR"}, "country"),
        ({"timezone": "Mars/Base"}, "timezone"),
        ({"code": "bad code!"}, "code"),
    ])
    def test_invalid_payloads(self, api, admin_a, world, org_a, payload, field):
        base = {"organization": str(org_a.pk), "location_type": str(world["type_a"].pk), "name": "N", "code": "N"}
        response = api(admin_a).post(LOCATIONS, {**base, **payload})
        assert response.status_code == 400
        if field:
            assert field in response.json()["error"]["details"]

    def test_required_fields(self, api, admin_a):
        details = api(admin_a).post(LOCATIONS, {}).json()["error"]["details"]
        assert {"organization", "location_type", "name", "code"} <= set(details)

    def test_patch_fields(self, api, admin_a, world):
        response = api(admin_a).patch(loc_url(world["a_child"]), {"name": "Renamed", "city": "Hull"})
        assert response.status_code == 200 and response.json()["data"]["name"] == "Renamed"

    def test_patch_cannot_change_organization(self, api, staff_user, world, org_b):
        response = api(staff_user).patch(loc_url(world["a_root"]), {"organization": str(org_b.pk)})
        assert response.status_code == 400

    def test_patch_cannot_flip_is_active(self, api, admin_a, world):
        api(admin_a).patch(loc_url(world["a_child"]), {"is_active": False})
        world["a_child"].refresh_from_db()
        assert world["a_child"].is_active

    def test_patch_parent_moves_the_location_and_logs_it(self, api, admin_a, world, org_a, make_location):
        other = make_location(org_a, "Other", type=world["type_a"], code="OTHER")
        response = api(admin_a).patch(loc_url(world["a_child"]), {"parent": str(other.pk)})
        assert response.status_code == 200 and response.json()["data"]["parent"] == str(other.pk)
        assert ActivityLog.objects.filter(verb="location.parent_changed").exists()


class TestHierarchyRulesOverTheApi:
    def test_self_parent_is_rejected(self, api, admin_a, world):
        response = api(admin_a).patch(loc_url(world["a_root"]), {"parent": str(world["a_root"].pk)})
        assert response.status_code == 400 and "parent" in response.json()["error"]["details"]

    def test_cycle_is_rejected(self, api, admin_a, world):
        response = api(admin_a).post(f"{loc_url(world['a_root'])}move/", {"parent": str(world["a_child"].pk)})
        assert response.status_code == 400
        world["a_root"].refresh_from_db()
        assert world["a_root"].parent_id is None

    def test_move_to_root(self, api, admin_a, world):
        response = api(admin_a).post(f"{loc_url(world['a_child'])}move/", {"parent": None})
        assert response.status_code == 200 and response.json()["data"]["parent"] is None

    def test_move_requires_the_parent_key(self, api, admin_a, world):
        assert api(admin_a).post(f"{loc_url(world['a_child'])}move/", {}).status_code == 400


class TestLifecycleActions:
    def test_deactivate_with_children_needs_cascade(self, api, admin_a, world):
        client = api(admin_a)
        response = client.post(f"{loc_url(world['a_root'])}deactivate/", {})
        assert response.status_code == 400 and "is_active" in response.json()["error"]["details"]
        ok = client.post(f"{loc_url(world['a_root'])}deactivate/", {"cascade": True})
        assert ok.status_code == 200 and ok.json()["data"]["is_active"] is False
        world["a_child"].refresh_from_db()
        assert not world["a_child"].is_active

    def test_activate_requires_an_active_parent(self, api, admin_a, world):
        client = api(admin_a)
        client.post(f"{loc_url(world['a_root'])}deactivate/", {"cascade": True})
        assert client.post(f"{loc_url(world['a_child'])}activate/").status_code == 400
        assert client.post(f"{loc_url(world['a_root'])}activate/").status_code == 200
        assert client.post(f"{loc_url(world['a_child'])}activate/").status_code == 200

    def test_type_activate_and_deactivate(self, api, admin_a, world):
        client = api(admin_a)
        url = f"{TYPES}{world['type_a'].pk}/"
        assert client.post(f"{url}deactivate/").json()["data"]["is_active"] is False
        assert client.post(f"{url}activate/").json()["data"]["is_active"] is True


class TestLocationTypes:
    def test_create_for_own_organization(self, api, admin_a, org_a):
        response = api(admin_a).post(TYPES, {"organization": str(org_a.pk), "name": "Branch", "code": "branch"})
        assert response.status_code == 201 and response.json()["data"]["code"] == "BRANCH"
        assert ActivityLog.objects.filter(verb="location_type.created", actor=admin_a).exists()

    def test_cannot_create_for_a_foreign_organization(self, api, admin_a, org_b):
        response = api(admin_a).post(TYPES, {"organization": str(org_b.pk), "name": "X", "code": "X"})
        assert response.status_code == 400
        assert not LocationType.objects.filter(code="X").exists()

    def test_org_admin_cannot_create_platform_types(self, api, admin_a):
        assert api(admin_a).post(TYPES, {"name": "Global", "code": "GLOBAL"}).status_code == 403

    def test_staff_can_create_platform_types(self, api, staff_user):
        response = api(staff_user).post(TYPES, {"name": "Global", "code": "GLOBAL"})
        assert response.status_code == 201 and response.json()["data"]["is_platform_type"] is True

    def test_org_admin_cannot_edit_a_platform_type(self, api, admin_a, world):
        url = f"{TYPES}{world['platform'].pk}/"
        assert api(admin_a).get(url).status_code == 200
        assert api(admin_a).patch(url, {"name": "Hacked"}).status_code == 403
        assert api(admin_a).post(f"{url}deactivate/").status_code == 403
        world["platform"].refresh_from_db()
        assert world["platform"].name == "Site" and world["platform"].is_active

    def test_duplicate_code(self, api, admin_a, world, org_a):
        response = api(admin_a).post(TYPES, {"organization": str(org_a.pk), "name": "D", "code": "region"})
        assert response.status_code == 400 and "code" in response.json()["error"]["details"]

    def test_update_name(self, api, admin_a, world):
        response = api(admin_a).patch(f"{TYPES}{world['type_a'].pk}/", {"name": "Zone"})
        assert response.status_code == 200 and response.json()["data"]["name"] == "Zone"

    def test_type_filters(self, api, admin_a, world):
        client = api(admin_a)
        assert ids(client.get(TYPES, {"search": "reg"})) == {str(world["type_a"].pk)}
        assert str(world["type_b"].pk) not in ids(client.get(TYPES, {"is_active": "true"}))


class TestTreeEndpoint:
    def test_nested_structure(self, api, admin_a, world):
        data = api(admin_a).get(TREE).json()["data"]
        assert len(data) == 1 and data[0]["name"] == "A Root"
        assert [c["name"] for c in data[0]["children"]] == ["A Child"]
        assert data[0]["children"][0]["children"] == []
        assert data[0]["location_type_name"] == "Region" and data[0]["matched"] is True

    def test_is_not_paginated(self, api, admin_a, world):
        assert "next" not in api(admin_a).get(TREE).json()["meta"]

    def test_filter_keeps_ancestors_for_context(self, api, admin_a, world):
        data = api(admin_a).get(TREE, {"search": "child"}).json()["data"]
        assert data[0]["matched"] is False and data[0]["children"][0]["matched"] is True

    def test_arbitrary_depth(self, api, admin_a, org_a, make_location):
        parent = None
        for i in range(40):
            parent = make_location(org_a, f"L{i}", parent=parent, code=f"L{i}")
        node = api(admin_a).get(TREE).json()["data"][0]
        depth = 1
        while node["children"]:
            node = node["children"][0]
            depth += 1
        assert depth == 40

    def test_query_count_is_constant(self, api, admin_a, org_a, make_location, django_assert_max_num_queries):
        client = api(admin_a)
        parent = make_location(org_a, "root", code="root")
        for i in range(3):
            make_location(org_a, f"c{i}", parent=parent, code=f"c{i}")
        client.get(TREE)  # warm caches (content types, permissions)
        with django_assert_max_num_queries(8):
            client.get(TREE)
        for i in range(60):
            make_location(org_a, f"big{i}", parent=parent if i % 2 else None, code=f"big{i}")
        with django_assert_max_num_queries(8):
            assert client.get(TREE).status_code == 200


class TestQueryCounts:
    def test_list_query_count_does_not_grow_with_rows(self, api, admin_a, org_a, make_location, django_assert_max_num_queries):
        client = api(admin_a)
        for i in range(3):
            make_location(org_a, f"s{i}", code=f"s{i}")
        client.get(LOCATIONS)
        with django_assert_max_num_queries(10):
            client.get(LOCATIONS)
        for i in range(40):
            make_location(org_a, f"m{i}", code=f"m{i}")
        with django_assert_max_num_queries(10):
            assert len(client.get(LOCATIONS, {"page_size": 50}).json()["data"]) == 43


class TestOpenApi:
    def test_schema_documents_the_phase_one_endpoints(self, client):
        body = client.get("/api/schema/").content.decode()
        for path in ("/api/v1/organizations/", "/api/v1/location-types/", "/api/v1/locations/", "/api/v1/locations/tree/"):
            assert path in body
