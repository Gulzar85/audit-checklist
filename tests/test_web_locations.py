"""Location pages: list, tree, detail, create, update, status, picker and types."""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.core.models import ActivityLog
from apps.locations import services
from apps.locations.models import Location, LocationType

pytestmark = pytest.mark.django_db


def login(client, user):
    client.force_login(user)
    return client


def body(response) -> str:
    return response.content.decode()


@pytest.fixture
def world(org_a, org_b, make_type, make_location):
    type_a = make_type(org_a, "Region", "REGION")
    type_b = make_type(org_b, "Division", "DIVISION")
    root = make_location(org_a, "Alpha Root", type=type_a, code="ALPHA", city="Leeds", country="GB")
    child = make_location(org_a, "Alpha Child", parent=root, type=type_a, code="ALPHA-1", city="York", country="GB")
    foreign = make_location(org_b, "Bravo Secret", type=type_b, code="BRAVO", city="Boston", country="US")
    return {"type_a": type_a, "type_b": type_b, "root": root, "child": child, "foreign": foreign}


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
class TestList:
    url = staticmethod(lambda: reverse("locations:list"))

    def test_requires_login(self, client):
        assert client.get(self.url()).status_code == 302

    def test_member_sees_only_their_locations(self, client, admin_a, world):
        content = body(login(client, admin_a).get(self.url()))
        assert "Alpha Root" in content and "Alpha Child" in content
        assert "Bravo Secret" not in content

    def test_staff_see_everything(self, client, staff_user, world):
        assert "Bravo Secret" in body(login(client, staff_user).get(self.url()))

    def test_filter_form_never_offers_other_tenants_data(self, client, admin_a, world):
        """The dropdown *choices* must not leak: only the user's own organization appears."""
        content = body(login(client, admin_a).get(self.url()))
        assert "Org B" not in content and "Division" not in content and "BRAVO" not in content

    @pytest.mark.parametrize(
        ("params", "present", "absent"),
        [
            ({"q": "child"}, ">Alpha Child</a>", ">Alpha Root</a>"),
            ({"q": "ALPHA-1"}, ">Alpha Child</a>", ">Alpha Root</a>"),
            ({"city": "leeds"}, ">Alpha Root</a>", ">Alpha Child</a>"),
            ({"country": "gb"}, ">Alpha Root</a>", None),
            ({"root": "true"}, ">Alpha Root</a>", ">Alpha Child</a>"),
            ({"status": "active"}, ">Alpha Root</a>", None),
        ],
    )
    def test_filters(self, client, admin_a, world, params, present, absent):
        content = body(login(client, admin_a).get(self.url(), params))
        assert present in content
        if absent:
            assert absent not in content

    def test_filter_by_type_and_parent(self, client, admin_a, world):
        content = body(login(client, admin_a).get(self.url(), {"parent": str(world["root"].pk)}))
        assert "Alpha Child" in content and "Alpha Root</a>" not in content
        content = body(client.get(self.url(), {"location_type": str(world["type_a"].pk)}))
        assert "Alpha Root" in content

    def test_inactive_filter(self, client, admin_a, world):
        services.deactivate_location(world["child"])
        content = body(login(client, admin_a).get(self.url(), {"status": "inactive"}))
        assert "Alpha Child" in content and "Alpha Root</a>" not in content

    def test_foreign_organization_filter_value_matches_nothing(self, client, admin_a, world, org_b):
        content = body(login(client, admin_a).get(self.url(), {"organization": str(org_b.pk)}))
        assert "Bravo Secret" not in content

    def test_foreign_parent_filter_value_matches_nothing(self, client, admin_a, world):
        content = body(login(client, admin_a).get(self.url(), {"parent": str(world["foreign"].pk)}))
        assert "Bravo Secret" not in content

    def test_malformed_filter_values_do_not_crash(self, client, admin_a, world):
        response = login(client, admin_a).get(self.url(), {"parent": "nope", "organization": "x", "page": "abc"})
        assert response.status_code in (200, 404)

    def test_pagination_and_sorting(self, client, admin_a, org_a, world, make_location):
        for i in range(30):
            make_location(org_a, f"Bulk {i:02d}", type=world["type_a"], code=f"BULK{i}")
        page1 = body(login(client, admin_a).get(self.url()))
        assert "Page 1 of 2" in page1
        assert "Page 2 of 2" in body(client.get(self.url(), {"page": 2}))
        descending = body(client.get(self.url(), {"sort": "-name"}))
        assert descending.index("Bulk 29") < descending.index("Bulk 28")

    def test_empty_state(self, client, make_user, org_a):
        user = make_user(organization=org_a)
        assert "No locations found" in body(login(client, user).get(self.url()))

    def test_row_actions_follow_permissions(self, client, viewer_a, admin_a, world):
        edit = reverse("locations:update", args=[world["root"].pk])
        assert edit not in body(login(client, viewer_a).get(self.url()))
        client.logout()
        assert edit in body(login(client, admin_a).get(self.url()))

    def test_query_count_is_constant_in_the_number_of_rows(
        self, client, admin_a, org_a, world, make_location, django_assert_max_num_queries
    ):
        login(client, admin_a)
        client.get(self.url())
        # Budget includes one bounded identity-context query per page (the
        # validated active-organization lookup for the header switcher); the
        # count still does not grow with the number of rows.
        with django_assert_max_num_queries(15):
            client.get(self.url())
        for i in range(25):
            make_location(org_a, f"Q{i}", type=world["type_a"], code=f"Q{i}", parent=world["root"])
        with django_assert_max_num_queries(15):
            assert client.get(self.url()).status_code == 200


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------
class TestTree:
    url = staticmethod(lambda: reverse("locations:tree"))

    def test_renders_the_hierarchy_and_isolates_tenants(self, client, admin_a, world):
        content = body(login(client, admin_a).get(self.url()))
        assert "Alpha Root" in content and "Alpha Child" in content and "Bravo Secret" not in content
        assert "<details" in content and "aria-label=\"Location hierarchy\"" in content

    def test_structure_is_balanced_html(self, client, admin_a, world):
        content = body(login(client, admin_a).get(self.url()))
        import re

        for tag in ("ul", "li", "details"):
            opened = len(re.findall(rf"<{tag}[\s>]", content))
            assert opened == content.count(f"</{tag}>"), tag

    def test_each_row_shows_type_and_status_not_just_colour(self, client, admin_a, world):
        content = body(login(client, admin_a).get(self.url()))
        assert "Region" in content and "Active" in content

    def test_htmx_requests_get_only_the_panel(self, client, admin_a, world):
        response = login(client, admin_a).get(self.url(), HTTP_HX_REQUEST="true")
        content = body(response)
        assert "Alpha Root" in content and "<html" not in content and "<nav" not in content
        assert "HX-Request" in response["Vary"]

    def test_htmx_history_restore_gets_the_full_page(self, client, admin_a, world):
        response = login(client, admin_a).get(
            self.url(), HTTP_HX_REQUEST="true", HTTP_HX_HISTORY_RESTORE_REQUEST="true"
        )
        assert "<html" in body(response)

    def test_search_keeps_ancestors_for_context(self, client, admin_a, world):
        content = body(login(client, admin_a).get(self.url(), {"q": "Alpha Child"}))
        assert "Alpha Root" in content and "(shown for context)" in content

    def test_filtered_empty_result_shows_the_no_match_state(self, client, admin_a, world):
        assert "No locations match" in body(login(client, admin_a).get(self.url(), {"q": "zzzz"}))

    def test_unfiltered_empty_state_offers_creation(self, client, admin_a, org_a):
        content = body(login(client, admin_a).get(self.url()))
        assert "No locations yet" in content and reverse("locations:create") in content

    def test_read_only_user_gets_no_add_links(self, client, viewer_a, world):
        content = body(login(client, viewer_a).get(self.url()))
        assert "Alpha Root" in content and reverse("locations:create") not in content

    def test_has_expand_collapse_controls_and_an_error_region(self, client, admin_a, world):
        content = body(login(client, admin_a).get(self.url()))
        assert "Expand all" in content and "Collapse all" in content and "data-htmx-error" in content

    def test_very_deep_hierarchies_render(self, client, admin_a, org_a, make_location):
        parent = None
        for i in range(150):
            parent = make_location(org_a, f"Lvl{i}", parent=parent, code=f"LVL{i}")
        response = login(client, admin_a).get(self.url())
        assert response.status_code == 200 and "Lvl149" in body(response)
        content = body(response)
        assert content.count("<details") == content.count("</details>") == 149

    def test_query_count_is_constant(self, client, admin_a, org_a, world, make_location, django_assert_max_num_queries):
        login(client, admin_a)
        client.get(self.url())
        # See TestList: one bounded identity-context query is included here too.
        with django_assert_max_num_queries(13):
            client.get(self.url())
        for i in range(60):
            make_location(org_a, f"T{i}", type=world["type_a"], code=f"T{i}", parent=world["root"] if i % 2 else world["child"])
        with django_assert_max_num_queries(13):
            assert client.get(self.url()).status_code == 200


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------
class TestDetail:
    def test_shows_hierarchy_context(self, client, admin_a, world):
        content = body(login(client, admin_a).get(reverse("locations:detail", args=[world["child"].pk])))
        assert "Alpha Child" in content and "Alpha Root" in content  # breadcrumb ancestor

    def test_foreign_location_is_404(self, client, admin_a, world):
        assert login(client, admin_a).get(reverse("locations:detail", args=[world["foreign"].pk])).status_code == 404

    def test_invalid_uuid_is_404(self, client, admin_a):
        assert login(client, admin_a).get("/locations/not-a-uuid/").status_code == 404

    def test_actions_follow_permissions(self, client, viewer_a, admin_a, world):
        url = reverse("locations:detail", args=[world["child"].pk])
        deactivate = reverse("locations:status", args=[world["child"].pk])
        assert deactivate not in body(login(client, viewer_a).get(url))
        client.logout()
        assert deactivate in body(login(client, admin_a).get(url))

    def test_deactivate_dialog_warns_about_active_descendants(self, client, admin_a, world):
        content = body(login(client, admin_a).get(reverse("locations:detail", args=[world["root"].pk])))
        assert 'data-require-cascade="true"' in content and "1</strong> active descendant" in content

    def test_leaf_dialog_has_no_cascade_requirement(self, client, admin_a, world):
        content = body(login(client, admin_a).get(reverse("locations:detail", args=[world["child"].pk])))
        assert "data-require-cascade" not in content

    def test_inactive_location_offers_reactivation(self, client, admin_a, world):
        services.deactivate_location(world["child"])
        content = body(login(client, admin_a).get(reverse("locations:detail", args=[world["child"].pk])))
        assert "inactive" in content and "Reactivate" in content

    def test_activity_is_shown(self, client, admin_a, world):
        services.update_location(world["child"], actor=admin_a, city="Hull")
        content = body(login(client, admin_a).get(reverse("locations:detail", args=[world["child"].pk])))
        assert "location.updated" in content


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
class TestCreate:
    url = staticmethod(lambda: reverse("locations:create"))

    def payload(self, world, org, **extra):
        return {"organization": str(org.pk), "location_type": str(world["type_a"].pk),
                "name": "Fresh", "code": "fresh-1", **extra}

    def test_viewer_is_forbidden(self, client, viewer_a, world, org_a):
        assert login(client, viewer_a).get(self.url()).status_code == 403
        assert client.post(self.url(), self.payload(world, org_a)).status_code == 403

    def test_admin_creates_a_root_location(self, client, admin_a, world, org_a):
        response = login(client, admin_a).post(self.url(), self.payload(world, org_a, country="gb"))
        location = Location.objects.get(name="Fresh")
        assert response.status_code == 302 and response["Location"] == location.get_absolute_url()
        assert (location.code, location.country, location.parent) == ("FRESH-1", "GB", None)
        assert ActivityLog.objects.filter(verb="location.created", actor=admin_a).exists()

    def test_create_with_a_parent(self, client, admin_a, world, org_a):
        login(client, admin_a).post(self.url(), self.payload(world, org_a, parent=str(world["root"].pk)))
        assert Location.objects.get(name="Fresh").parent == world["root"]

    def test_add_child_prefills_organization_type_and_parent(self, client, admin_a, world):
        content = body(login(client, admin_a).get(self.url(), {"parent": str(world["root"].pk)}))
        assert "Alpha Root [ALPHA]" in content and "Org A" in content

    def test_prefill_ignores_a_foreign_parent(self, client, admin_a, world):
        content = body(login(client, admin_a).get(self.url(), {"parent": str(world["foreign"].pk)}))
        assert "Bravo Secret" not in content and "BRAVO" not in content

    def test_prefill_ignores_garbage(self, client, admin_a):
        assert login(client, admin_a).get(self.url(), {"parent": "zzz", "organization": "yyy"}).status_code == 200

    def test_duplicate_code_keeps_input(self, client, admin_a, world, org_a):
        response = login(client, admin_a).post(self.url(), self.payload(world, org_a, code="alpha", name="Kept"))
        content = body(response)
        assert response.status_code == 200 and "already exists in this organization" in content
        assert 'value="Kept"' in content and 'aria-invalid="true"' in content

    def test_tampered_foreign_parent_is_rejected(self, client, admin_a, world, org_a):
        response = login(client, admin_a).post(self.url(), self.payload(world, org_a, parent=str(world["foreign"].pk)))
        assert response.status_code == 200 and "Select a valid choice" in body(response)
        assert "Bravo Secret" not in body(response) and "BRAVO" not in body(response)  # no label leak
        assert not Location.objects.filter(name="Fresh").exists()

    def test_tampered_foreign_type_is_rejected(self, client, admin_a, world, org_a):
        data = self.payload(world, org_a, location_type=str(world["type_b"].pk))
        response = login(client, admin_a).post(self.url(), data)
        assert response.status_code == 200 and not Location.objects.filter(name="Fresh").exists()

    def test_tampered_foreign_organization_is_rejected(self, client, admin_a, world, org_b):
        data = {"organization": str(org_b.pk), "location_type": str(world["type_b"].pk), "name": "Fresh", "code": "F"}
        response = login(client, admin_a).post(self.url(), data)
        assert response.status_code == 200 and not Location.objects.filter(name="Fresh").exists()

    def test_inactive_type_is_not_offered_or_accepted(self, client, admin_a, world, org_a, make_type):
        retired = make_type(org_a, "Retired", "RETIRED", is_active=False)
        response = login(client, admin_a).post(self.url(), self.payload(world, org_a, location_type=str(retired.pk)))
        assert response.status_code == 200 and not Location.objects.filter(name="Fresh").exists()

    def test_inactive_organization_cannot_gain_locations(self, client, staff_user, world, org_a):
        from apps.organizations import services as org_services

        org_services.suspend_organization(org_a)
        response = login(client, staff_user).post(self.url(), self.payload(world, org_a))
        assert response.status_code in (200,) and not Location.objects.filter(name="Fresh").exists()

    def test_field_validation_messages(self, client, admin_a, world, org_a):
        response = login(client, admin_a).post(self.url(), self.payload(world, org_a, latitude="95", longitude="1"))
        assert "less than or equal to 90" in body(response)

    def test_form_labels_are_associated_with_controls(self, client, admin_a, world):
        import re

        content = body(login(client, admin_a).get(self.url()))
        labelled = set(re.findall(r'<label[^>]*for="([^"]+)"', content))
        controls = set(re.findall(r'<(?:input|select|textarea)[^>]*?id="([^"]+)"', content))
        required = {"id_name", "id_code", "id_organization", "id_location_type", "id_parent"}
        assert required <= labelled and required <= controls

    def test_organization_select_drives_the_htmx_refresh(self, client, admin_a, world):
        content = body(login(client, admin_a).get(self.url()))
        assert 'hx-get="/locations/form-relations/"' in content and 'hx-target="#relations"' in content


# ---------------------------------------------------------------------------
# Update / move
# ---------------------------------------------------------------------------
class TestUpdate:
    def url(self, location):
        return reverse("locations:update", args=[location.pk])

    def data(self, location, **extra):
        data = {"organization": str(location.organization_id), "location_type": str(location.location_type_id),
                "name": location.name, "code": location.code}
        if location.parent_id:
            data["parent"] = str(location.parent_id)
        return {**data, **extra}

    def test_viewer_is_forbidden(self, client, viewer_a, world):
        assert login(client, viewer_a).get(self.url(world["child"])).status_code == 403

    def test_foreign_location_is_404(self, client, admin_a, world):
        assert login(client, admin_a).get(self.url(world["foreign"])).status_code == 404
        assert client.post(self.url(world["foreign"]), {"name": "x"}).status_code == 404

    def test_edit_details(self, client, admin_a, world):
        response = login(client, admin_a).post(self.url(world["child"]), self.data(world["child"], name="Renamed", city="Hull"))
        assert response.status_code == 302
        world["child"].refresh_from_db()
        assert (world["child"].name, world["child"].city) == ("Renamed", "Hull")

    def test_move_to_a_new_parent_is_logged(self, client, admin_a, world, org_a, make_location):
        elsewhere = make_location(org_a, "Elsewhere", type=world["type_a"], code="ELSE")
        login(client, admin_a).post(self.url(world["child"]), self.data(world["child"], parent=str(elsewhere.pk)))
        world["child"].refresh_from_db()
        assert world["child"].parent == elsewhere
        assert ActivityLog.objects.filter(verb="location.parent_changed", actor=admin_a).exists()

    def test_move_to_root_by_clearing_the_parent(self, client, admin_a, world):
        data = self.data(world["child"])
        data.pop("parent")
        login(client, admin_a).post(self.url(world["child"]), data)
        world["child"].refresh_from_db()
        assert world["child"].parent is None

    def test_descendants_are_not_offered_and_are_rejected(self, client, admin_a, world):
        response = login(client, admin_a).post(self.url(world["root"]), self.data(world["root"], parent=str(world["child"].pk)))
        assert response.status_code == 200 and "Select a valid choice" in body(response)
        world["root"].refresh_from_db()
        assert world["root"].parent is None

    def test_self_parent_is_rejected(self, client, admin_a, world):
        response = login(client, admin_a).post(self.url(world["root"]), self.data(world["root"], parent=str(world["root"].pk)))
        assert response.status_code == 200
        world["root"].refresh_from_db()
        assert world["root"].parent is None

    def test_organization_cannot_be_changed(self, client, admin_a, world, org_b):
        login(client, admin_a).post(self.url(world["child"]), self.data(world["child"], organization=str(org_b.pk)))
        world["child"].refresh_from_db()
        assert world["child"].organization_id != org_b.pk

    def test_organization_field_is_disabled_on_edit(self, client, admin_a, world):
        content = body(login(client, admin_a).get(self.url(world["child"])))
        assert "disabled" in content.split('name="organization"')[1].split(">")[0]


# ---------------------------------------------------------------------------
# Activate / deactivate
# ---------------------------------------------------------------------------
class TestStatus:
    def url(self, location):
        return reverse("locations:status", args=[location.pk])

    def test_deactivate_a_leaf(self, client, admin_a, world):
        response = login(client, admin_a).post(self.url(world["child"]), {"action": "deactivate"})
        assert response.status_code == 302
        world["child"].refresh_from_db()
        assert not world["child"].is_active

    def test_parent_needs_the_cascade_option(self, client, admin_a, world):
        login(client, admin_a).post(self.url(world["root"]), {"action": "deactivate"})
        world["root"].refresh_from_db()
        assert world["root"].is_active  # refused
        client.post(self.url(world["root"]), {"action": "deactivate", "cascade": "1"})
        world["root"].refresh_from_db(), world["child"].refresh_from_db()
        assert not world["root"].is_active and not world["child"].is_active

    def test_refusal_is_explained_to_the_user(self, client, admin_a, world):
        response = login(client, admin_a).post(self.url(world["root"]), {"action": "deactivate"}, follow=True)
        assert "active descendant" in body(response)

    def test_activate(self, client, admin_a, world):
        services.deactivate_location(world["child"])
        login(client, admin_a).post(self.url(world["child"]), {"action": "activate"})
        world["child"].refresh_from_db()
        assert world["child"].is_active

    def test_viewer_is_forbidden(self, client, viewer_a, world):
        assert login(client, viewer_a).post(self.url(world["child"]), {"action": "deactivate"}).status_code == 403
        world["child"].refresh_from_db()
        assert world["child"].is_active

    def test_foreign_location_is_404(self, client, admin_a, world):
        assert login(client, admin_a).post(self.url(world["foreign"]), {"action": "deactivate"}).status_code == 404

    def test_get_is_not_allowed_and_unknown_actions_are_rejected(self, client, admin_a, world):
        assert login(client, admin_a).get(self.url(world["child"])).status_code == 405
        client.post(self.url(world["child"]), {"action": "explode"})
        world["child"].refresh_from_db()
        assert world["child"].is_active


# ---------------------------------------------------------------------------
# HTMX fragments
# ---------------------------------------------------------------------------
class TestPicker:
    url = staticmethod(lambda: reverse("locations:picker"))

    def test_requires_login(self, client):
        assert client.get(self.url()).status_code == 302

    def test_searches_within_the_visible_scope(self, client, admin_a, world, org_a):
        content = body(login(client, admin_a).get(self.url(), {"organization": str(org_a.pk), "q": "alpha"}))
        assert "Alpha Root" in content and "Alpha Child" in content

    def test_never_returns_other_tenants_data(self, client, admin_a, world, org_b):
        for params in ({"q": "bravo"}, {"organization": str(org_b.pk)}, {"organization": str(org_b.pk), "q": "bravo"}):
            assert "Bravo Secret" not in body(login(client, admin_a).get(self.url(), params)), params

    def test_staff_can_search_across_organizations(self, client, staff_user, world):
        assert "Bravo Secret" in body(login(client, staff_user).get(self.url(), {"q": "bravo"}))

    def test_excludes_a_location_and_its_subtree(self, client, admin_a, world, org_a):
        content = body(login(client, admin_a).get(self.url(), {"organization": str(org_a.pk), "exclude": str(world["root"].pk)}))
        assert "Alpha Root" not in content and "Alpha Child" not in content

    def test_only_active_locations_by_default(self, client, admin_a, world, org_a):
        services.deactivate_location(world["child"])
        params = {"organization": str(org_a.pk), "q": "child"}
        assert "Alpha Child" not in body(login(client, admin_a).get(self.url(), params))
        assert "Alpha Child" in body(client.get(self.url(), {**params, "include_inactive": "1"}))

    def test_asks_for_an_organization_when_required(self, client, admin_a, world):
        content = body(login(client, admin_a).get(self.url(), {"require_organization": "1"}))
        assert "Choose an organization first" in content and "Alpha" not in content

    def test_results_are_capped(self, client, admin_a, org_a, world, make_location):
        for i in range(30):
            make_location(org_a, f"Many {i:02d}", type=world["type_a"], code=f"MANY{i}")
        content = body(login(client, admin_a).get(self.url(), {"organization": str(org_a.pk), "q": "many"}))
        assert content.count('role="option"') == 20 and "Showing the first results" in content

    def test_malformed_parameters_are_harmless(self, client, admin_a, world):
        response = login(client, admin_a).get(self.url(), {"organization": "x", "exclude": "y", "q": "%" * 500})
        assert response.status_code == 200

    def test_output_is_escaped(self, client, admin_a, org_a, world, make_location):
        make_location(org_a, '<script>alert(1)</script>', type=world["type_a"], code="XSS")
        content = body(login(client, admin_a).get(self.url(), {"organization": str(org_a.pk), "q": "script"}))
        assert "<script>alert(1)</script>" not in content and "&lt;script&gt;" in content


class TestRelationsFragment:
    def test_scopes_type_and_parent_to_the_organization(self, client, admin_a, world, org_a):
        content = body(login(client, admin_a).get(reverse("locations:form_relations"), {"organization": str(org_a.pk)}))
        assert "Region" in content and "Division" not in content

    def test_foreign_organization_yields_no_options(self, client, admin_a, world, org_b):
        content = body(login(client, admin_a).get(reverse("locations:form_relations"), {"organization": str(org_b.pk)}))
        assert "Division" not in content


# ---------------------------------------------------------------------------
# Location types
# ---------------------------------------------------------------------------
class TestLocationTypePages:
    def test_list_shows_own_and_platform_types_only(self, client, admin_a, world, make_type):
        make_type(None, "Platform Site", "PSITE")
        content = body(login(client, admin_a).get(reverse("locations:type_list")))
        assert "Region" in content and "Platform Site" in content and "Division" not in content

    def test_create_for_own_organization(self, client, admin_a, org_a):
        response = login(client, admin_a).post(
            reverse("locations:type_create"),
            {"organization": str(org_a.pk), "name": "Branch", "code": "branch", "sort_order": 3},
        )
        assert response.status_code == 302
        assert LocationType.objects.get(code="BRANCH").organization == org_a

    def test_org_admin_cannot_create_platform_types(self, client, admin_a):
        response = login(client, admin_a).post(
            reverse("locations:type_create"), {"organization": "", "name": "Global", "code": "global", "sort_order": 0}
        )
        assert response.status_code == 200
        assert "do not have permission" in body(response) and not LocationType.objects.filter(code="GLOBAL").exists()

    def test_staff_can_create_platform_types(self, client, staff_user):
        login(client, staff_user).post(
            reverse("locations:type_create"), {"organization": "", "name": "Global", "code": "global", "sort_order": 0}
        )
        assert LocationType.objects.get(code="GLOBAL").is_platform_type

    def test_cannot_create_for_a_foreign_organization(self, client, admin_a, org_b):
        response = login(client, admin_a).post(
            reverse("locations:type_create"), {"organization": str(org_b.pk), "name": "X", "code": "x", "sort_order": 0}
        )
        assert response.status_code == 200 and not LocationType.objects.filter(code="X").exists()

    def test_viewer_cannot_create_or_edit(self, client, viewer_a, world):
        assert login(client, viewer_a).get(reverse("locations:type_create")).status_code == 403
        assert client.get(reverse("locations:type_update", args=[world["type_a"].pk])).status_code == 403

    def test_edit_and_toggle(self, client, admin_a, world):
        edit = reverse("locations:type_update", args=[world["type_a"].pk])
        data = {"organization": str(world["type_a"].organization_id), "name": "Zone", "code": "REGION", "sort_order": 1}
        assert login(client, admin_a).post(edit, data).status_code == 302
        world["type_a"].refresh_from_db()
        assert world["type_a"].name == "Zone"
        client.post(reverse("locations:type_status", args=[world["type_a"].pk]), {"action": "deactivate"})
        world["type_a"].refresh_from_db()
        assert not world["type_a"].is_active

    def test_foreign_type_is_404(self, client, admin_a, world):
        assert login(client, admin_a).get(reverse("locations:type_update", args=[world["type_b"].pk])).status_code == 404
        assert client.post(reverse("locations:type_status", args=[world["type_b"].pk]), {"action": "deactivate"}).status_code == 404

    def test_org_admin_cannot_edit_platform_types(self, client, admin_a, make_type):
        platform = make_type(None, "Platform", "PLAT")
        assert login(client, admin_a).get(reverse("locations:type_update", args=[platform.pk])).status_code == 403
        assert client.post(reverse("locations:type_status", args=[platform.pk]), {"action": "deactivate"}).status_code == 403
        platform.refresh_from_db()
        assert platform.is_active

    def test_organization_field_is_fixed_on_edit(self, client, admin_a, world):
        content = body(login(client, admin_a).get(reverse("locations:type_update", args=[world["type_a"].pk])))
        assert "disabled" in content.split('name="organization"')[1].split(">")[0]
