"""Location services: creation, updates, moves, lifecycle, history and logging."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from apps.core.models import ActivityLog
from apps.locations import services
from apps.locations.models import Location
from apps.locations.selectors import get_descendants

pytestmark = pytest.mark.django_db


class TestCreation:
    def test_create_root_and_child(self, org_a, make_type, admin_a):
        region = make_type(org_a, "Region", "REGION")
        root = services.create_location(
            organization=org_a, location_type=region, name="North", code="north", actor=admin_a
        )
        child = services.create_location(
            organization=org_a, location_type=region, name="North East", code="NE", parent=root
        )
        assert root.parent is None and root.code == "NORTH"
        assert child.parent == root
        assert list(root.children.all()) == [child]

    def test_new_locations_start_active_even_if_asked_otherwise(self, org_a, make_type):
        location = services.create_location(
            organization=org_a, location_type=make_type(org_a), name="X", code="X", is_active=False
        )
        assert location.is_active

    def test_creation_is_logged_and_recorded_in_history(self, org_a, make_type, admin_a):
        location = services.create_location(
            organization=org_a, location_type=make_type(org_a), name="X", code="X", actor=admin_a
        )
        entry = ActivityLog.objects.get(verb="location.created")
        assert (entry.organization, entry.actor, entry.target_id) == (org_a, admin_a, str(location.pk))
        assert location.history.first().history_user == admin_a

    def test_organization_is_required(self, make_type, org_a):
        with pytest.raises(ValidationError) as exc:
            services.create_location(location_type=make_type(org_a), name="X", code="X")
        assert "organization" in exc.value.message_dict

    def test_location_type_is_required(self, org_a):
        with pytest.raises(ValidationError) as exc:
            services.create_location(organization=org_a, name="X", code="X")
        assert "location_type" in exc.value.message_dict

    def test_country_is_normalised_and_validated(self, org_a, make_type):
        ok = services.create_location(
            organization=org_a, location_type=make_type(org_a), name="X", code="X", country="gb"
        )
        assert ok.country == "GB"
        with pytest.raises(ValidationError) as exc:
            services.create_location(
                organization=org_a, location_type=ok.location_type, name="Y", code="Y", country="GBR"
            )
        assert "country" in exc.value.message_dict

    def test_inactive_organization_refuses_new_locations(self, org_a, make_type):
        lt = make_type(org_a)
        org_a.status = "inactive"
        org_a.save()
        with pytest.raises(ValidationError) as exc:
            services.create_location(organization=org_a, location_type=lt, name="X", code="X")
        assert "organization" in exc.value.message_dict
        assert not Location.objects.exists()

    def test_cannot_create_under_an_inactive_parent(self, org_a, make_location):
        parent = make_location(org_a, "Parent")
        services.deactivate_location(parent)
        with pytest.raises(ValidationError) as exc:
            services.create_location(
                organization=org_a, location_type=parent.location_type, parent=parent, name="X", code="X"
            )
        assert "parent" in exc.value.message_dict


class TestUpdate:
    def test_update_logs_changes(self, org_a, make_location, admin_a):
        location = make_location(org_a, "Old", city="Leeds")
        services.update_location(location, actor=admin_a, name="New", city="York")
        entry = ActivityLog.objects.get(verb="location.updated")
        assert entry.metadata["changes"] == {
            "name": {"from": "Old", "to": "New"},
            "city": {"from": "Leeds", "to": "York"},
        }
        assert location.history.first().history_change_reason == "location.updated"

    def test_noop_update_logs_nothing(self, org_a, make_location):
        location = make_location(org_a, "Same")
        before = ActivityLog.objects.count()
        services.update_location(location, name="Same")
        assert ActivityLog.objects.count() == before

    def test_is_active_is_not_editable_through_update(self, org_a, make_location):
        with pytest.raises(ValueError):
            services.update_location(make_location(org_a, "X"), is_active=False)

    def test_changing_to_an_inactive_type_is_rejected(self, org_a, make_location, make_type):
        location = make_location(org_a, "X")
        retired = make_type(org_a, "Retired", "RETIRED", is_active=False)
        with pytest.raises(ValidationError) as exc:
            services.update_location(location, location_type=retired)
        assert "location_type" in exc.value.message_dict

    def test_keeping_an_already_inactive_type_is_fine(self, org_a, make_location, make_type):
        retired = make_type(org_a, "Retired", "RETIRED")
        location = make_location(org_a, "X", type=retired)
        services.deactivate_location_type(retired)
        services.update_location(location, name="Renamed")
        location.refresh_from_db()
        assert location.name == "Renamed"

    def test_update_returns_fresh_state(self, org_a, make_location):
        location = make_location(org_a, "X")
        updated = services.update_location(location, name="Y")
        assert updated.name == "Y"


class TestMove:
    def test_move_logs_parent_change_with_names(self, org_a, make_location, admin_a):
        old, new, child = (make_location(org_a, n) for n in ("Old", "New", "Child"))
        child.parent = old
        child.save()
        services.move_location(child, new, actor=admin_a)
        entry = ActivityLog.objects.get(verb="location.parent_changed")
        assert entry.metadata["from_parent"] == str(old.pk)
        assert entry.metadata["to_parent"] == str(new.pk)
        assert (entry.metadata["from_parent_name"], entry.metadata["to_parent_name"]) == ("Old", "New")
        assert child.history.first().history_change_reason == "location.parent_changed"

    def test_move_to_the_same_parent_is_a_noop(self, org_a, make_location):
        parent = make_location(org_a, "P")
        child = make_location(org_a, "C", parent=parent)
        before = ActivityLog.objects.count()
        services.move_location(child, parent)
        assert ActivityLog.objects.count() == before

    def test_move_carries_the_whole_subtree(self, chain_a, org_a, make_location):
        country, region, area, branch, store = chain_a
        elsewhere = make_location(org_a, "Elsewhere")
        services.move_location(region, elsewhere)
        assert set(get_descendants(elsewhere)) == {region, area, branch, store}
        assert not get_descendants(country).exists()

    def test_cannot_move_under_an_inactive_parent(self, org_a, make_location):
        parent = make_location(org_a, "P")
        child = make_location(org_a, "C")
        services.deactivate_location(parent)
        with pytest.raises(ValidationError) as exc:
            services.move_location(child, parent)
        assert "parent" in exc.value.message_dict

    def test_move_and_edit_in_one_update_log_two_events(self, org_a, make_location):
        parent = make_location(org_a, "P")
        child = make_location(org_a, "C")
        services.update_location(child, parent=parent, name="Renamed")
        assert ActivityLog.objects.filter(verb="location.parent_changed").count() == 1
        assert ActivityLog.objects.filter(verb="location.updated").count() == 1


class TestDeactivation:
    def test_deactivate_a_leaf(self, org_a, make_location, admin_a):
        leaf = make_location(org_a, "Leaf")
        services.deactivate_location(leaf, actor=admin_a)
        leaf.refresh_from_db()
        assert not leaf.is_active
        entry = ActivityLog.objects.get(verb="location.deactivated")
        assert entry.metadata["cascade"] is False
        assert leaf.history.first().history_user == admin_a

    def test_locations_are_never_deleted_by_deactivation(self, org_a, make_location):
        leaf = make_location(org_a, "Leaf")
        services.deactivate_location(leaf)
        assert Location.objects.filter(pk=leaf.pk).exists()

    def test_parent_with_active_children_is_refused_without_cascade(self, chain_a):
        country, *rest = chain_a
        with pytest.raises(ValidationError) as exc:
            services.deactivate_location(country)
        assert "is_active" in exc.value.message_dict
        assert Location.objects.filter(is_active=True).count() == 5

    def test_cascade_deactivates_the_whole_active_subtree_in_one_step(self, chain_a):
        country, region, *rest = chain_a
        services.deactivate_location(region, cascade=True)
        active = set(Location.objects.filter(is_active=True))
        assert active == {country}
        entry = ActivityLog.objects.get(verb="location.deactivated")
        assert entry.metadata["cascade"] is True
        assert entry.metadata["descendant_count"] == 3

    def test_cascade_keeps_per_node_history(self, chain_a, admin_a):
        _, region, area, *_ = chain_a
        services.deactivate_location(region, cascade=True, actor=admin_a)
        latest = area.history.first()
        assert latest.is_active is False
        assert latest.history_user == admin_a
        assert latest.history_change_reason == "location.deactivated"

    def test_cascade_is_all_or_nothing(self, chain_a, monkeypatch):
        from apps.locations import services as svc

        _, region, *_ = chain_a

        def boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(svc, "log_activity", boom)
        with pytest.raises(RuntimeError):
            services.deactivate_location(region, cascade=True)
        assert Location.objects.filter(is_active=True).count() == 5

    def test_deactivating_twice_is_a_noop(self, org_a, make_location):
        leaf = make_location(org_a, "Leaf")
        services.deactivate_location(leaf)
        before = ActivityLog.objects.count()
        services.deactivate_location(leaf)
        assert ActivityLog.objects.count() == before

    def test_deactivation_is_refused_while_the_organization_is_inactive(self, org_a, make_location):
        leaf = make_location(org_a, "Leaf")
        org_a.status = "suspended"
        org_a.save()
        with pytest.raises(ValidationError):
            services.deactivate_location(leaf)


class TestActivation:
    def test_reactivate_a_leaf(self, org_a, make_location, admin_a):
        leaf = make_location(org_a, "Leaf")
        services.deactivate_location(leaf)
        services.activate_location(leaf, actor=admin_a)
        leaf.refresh_from_db()
        assert leaf.is_active
        assert ActivityLog.objects.filter(verb="location.activated", actor=admin_a).exists()

    def test_cannot_activate_beneath_an_inactive_parent(self, chain_a):
        country, region, *_ = chain_a
        services.deactivate_location(country, cascade=True)
        with pytest.raises(ValidationError) as exc:
            services.activate_location(region)
        assert "is_active" in exc.value.message_dict

    def test_activating_top_down_restores_a_branch(self, chain_a):
        country, region, *_ = chain_a
        services.deactivate_location(country, cascade=True)
        services.activate_location(country)
        services.activate_location(region)
        assert Location.objects.filter(is_active=True).count() == 2

    def test_model_validation_blocks_active_child_under_inactive_parent(self, org_a, make_location):
        parent = make_location(org_a, "P")
        child = make_location(org_a, "C", parent=parent)
        Location.objects.filter(pk=parent.pk).update(is_active=False)
        child.refresh_from_db()
        with pytest.raises(ValidationError):
            child.full_clean()

    def test_model_validation_blocks_deactivating_a_parent_with_active_children(
        self, org_a, make_location
    ):
        parent = make_location(org_a, "P")
        make_location(org_a, "C", parent=parent)
        parent.is_active = False
        with pytest.raises(ValidationError) as exc:
            parent.full_clean()
        assert "is_active" in exc.value.message_dict


class TestHierarchyShape:
    """The schema supports different shapes per organization, at any depth."""

    def test_two_organizations_with_different_type_vocabularies(
        self, org_a, org_b, make_type
    ):
        def build(org, names):
            parent = None
            for name in names:
                lt = make_type(org, name, name.upper().replace(" ", "_"))
                parent = services.create_location(
                    organization=org,
                    location_type=lt,
                    parent=parent,
                    name=f"{name} 1",
                    code=f"{lt.code}-1",
                )
            return parent

        leaf_a = build(org_a, ["Country", "Region", "Area", "Branch", "Location"])
        leaf_b = build(org_b, ["Business Unit", "Division", "Facility", "Department"])
        assert [a.location_type.name for a in leaf_a.get_ancestors()] == [
            "Country", "Region", "Area", "Branch",
        ]
        assert [a.location_type.name for a in leaf_b.get_ancestors()] == [
            "Business Unit", "Division", "Facility",
        ]

    def test_no_hard_coded_maximum_depth(self, org_a, make_location):
        parent = None
        for i in range(60):
            parent = make_location(org_a, f"Level {i}", parent=parent)
        assert parent.depth == 59
        assert Location.objects.count() == 60

    def test_there_is_no_industry_specific_hierarchy_model(self):
        from django.apps import apps

        names = {m.__name__.lower() for m in apps.get_app_config("locations").get_models()}
        assert not names & {"region", "area", "branch", "restaurant", "store", "warehouse"}
