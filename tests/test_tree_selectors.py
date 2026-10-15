"""Hierarchy reads: roots, children, ancestors, descendants and tree assembly."""

from __future__ import annotations

import pytest

from apps.locations import selectors
from apps.locations.models import Location

pytestmark = pytest.mark.django_db


def _names(items):
    return [item.name for item in items]


class TestBasicReads:
    def test_root_locations(self, org_a, org_b, chain_a, make_location):
        other_root = make_location(org_a, "Second root")
        make_location(org_b, "Foreign root")
        roots = selectors.get_root_locations(org_a)
        assert set(roots) == {chain_a[0], other_root}

    def test_children_are_direct_only(self, chain_a):
        country, region, *_ = chain_a
        assert list(selectors.get_children(country)) == [region]

    def test_locations_for_organization_is_isolated(self, org_a, org_b, chain_a, make_location):
        foreign = make_location(org_b, "Foreign")
        found = selectors.get_locations_for_organization(org_a)
        assert set(found) == set(chain_a)
        assert foreign not in found

    def test_active_locations(self, org_a, chain_a):
        Location.objects.filter(pk=chain_a[-1].pk).update(is_active=False)
        active = selectors.get_active_locations(org_a)
        assert chain_a[-1] not in active
        assert active.count() == 4

    def test_active_locations_without_an_organization_spans_all(self, chain_a, org_b, make_location):
        make_location(org_b, "Foreign")
        assert selectors.get_active_locations().count() == 6


class TestAncestorsAndDescendants:
    def test_ancestors_are_ordered_root_first(self, chain_a):
        store = chain_a[-1]
        assert _names(selectors.get_ancestors(store)) == [
            "Country 1", "Region 1", "Area 1", "Branch 1",
        ]

    def test_a_root_has_no_ancestors(self, chain_a):
        assert selectors.get_ancestors(chain_a[0]) == []

    def test_descendants_exclude_self_by_default(self, chain_a):
        country, region, area, branch, store = chain_a
        assert set(selectors.get_descendants(country)) == {region, area, branch, store}
        assert set(selectors.get_descendants(area)) == {branch, store}
        assert not selectors.get_descendants(store).exists()

    def test_descendants_can_include_self(self, chain_a):
        area = chain_a[2]
        assert area in selectors.get_descendants(area, include_self=True)

    def test_descendants_is_a_composable_queryset(self, chain_a):
        country = chain_a[0]
        Location.objects.filter(pk=chain_a[-1].pk).update(is_active=False)
        assert selectors.get_descendants(country).filter(is_active=True).count() == 3
        assert selectors.get_descendants(country).order_by("-name").first().name == "Store 1"

    def test_descendants_across_branches(self, org_a, make_location):
        root = make_location(org_a, "root")
        left = make_location(org_a, "left", parent=root)
        right = make_location(org_a, "right", parent=root)
        leaf = make_location(org_a, "leaf", parent=left)
        assert set(selectors.get_descendants(root)) == {left, right, leaf}
        assert set(selectors.get_descendants(left)) == {leaf}

    def test_many_roots_at_once_for_scoped_access(self, org_a, make_location):
        """A user scoped to two subtrees (Phase 4) resolves both in one query."""
        r1, r2, r3 = (make_location(org_a, f"r{i}") for i in range(3))
        c1 = make_location(org_a, "c1", parent=r1)
        c2 = make_location(org_a, "c2", parent=r2)
        c3 = make_location(org_a, "c3", parent=r3)
        ids = selectors.get_descendant_ids([r1, r2], include_roots=True)
        assert ids == {r1.pk, r2.pk, c1.pk, c2.pk}
        assert c3.pk not in ids

    def test_ancestor_ids_for_many(self, chain_a):
        assert selectors.get_ancestor_ids([chain_a[1], chain_a[3]]) == {
            chain_a[0].pk, chain_a[1].pk, chain_a[2].pk,
        }

    def test_empty_input_runs_no_query(self, django_assert_num_queries):
        with django_assert_num_queries(0):
            assert selectors.get_descendant_ids([]) == set()
            assert selectors.get_ancestor_ids([]) == set()

    def test_deep_hierarchy_to_depth_fifty(self, org_a, make_location):
        parent, nodes = None, []
        for i in range(50):
            parent = make_location(org_a, f"n{i}", parent=parent)
            nodes.append(parent)
        assert len(selectors.get_ancestors(nodes[-1])) == 49
        assert len(selectors.get_descendants(nodes[0])) == 49

    def test_recursion_terminates_even_on_corrupt_cyclic_data(self, org_a, make_location):
        """UNION (not UNION ALL) means a cycle written past the guards cannot hang a read."""
        from django.db import connection

        a = make_location(org_a, "A")
        b = make_location(org_a, "B", parent=a)
        with connection.cursor() as cursor:
            # 'replica' mode suppresses triggers, simulating legacy/corrupt data.
            cursor.execute("SET LOCAL session_replication_role = 'replica'")
            try:
                cursor.execute("UPDATE locations_location SET parent_id = %s WHERE id = %s", [b.pk, a.pk])
            finally:
                cursor.execute("SET LOCAL session_replication_role = 'origin'")
        assert set(selectors.get_descendants(a)) == {a, b}
        assert selectors.get_ancestor_ids([a]) == {a.pk, b.pk}


class TestTreeAssembly:
    def test_builds_nested_forest(self, chain_a, org_a, make_location):
        extra = make_location(org_a, "Another root")
        roots = selectors.get_location_tree(selectors.get_locations_for_organization(org_a))
        assert {n.location for n in roots} == {chain_a[0], extra}
        node = next(n for n in roots if n.location == chain_a[0])
        depth = 0
        while node.children:
            assert len(node.children) == 1
            node = node.children[0]
            depth += 1
        assert depth == 4

    def test_siblings_are_sorted_by_name(self, org_a, make_location):
        root = make_location(org_a, "root")
        for name in ("charlie", "Alpha", "bravo"):
            make_location(org_a, name, parent=root)
        (node,) = selectors.get_location_tree(selectors.get_locations_for_organization(org_a))
        assert _names(c.location for c in node.children) == ["Alpha", "bravo", "charlie"]

    def test_filtered_tree_keeps_ancestors_for_context(self, chain_a, org_a):
        queryset = Location.objects.filter(organization=org_a, name="Branch 1")
        (root,) = selectors.get_location_tree(queryset)
        assert root.location == chain_a[0] and root.matched is False
        branch = root.children[0].children[0].children[0]
        assert branch.location == chain_a[3] and branch.matched is True

    def test_filtered_tree_without_ancestors_promotes_matches_to_roots(self, chain_a, org_a):
        queryset = Location.objects.filter(organization=org_a, name="Branch 1")
        (root,) = selectors.get_location_tree(queryset, include_ancestors=False)
        assert root.location == chain_a[3]

    def test_empty_queryset(self):
        assert selectors.get_location_tree(Location.objects.none()) == []


class TestQueryCounts:
    def test_tree_query_count_does_not_grow_with_the_tree(self, org_a, make_location, django_assert_max_num_queries):
        parent = None
        for i in range(5):
            parent = make_location(org_a, f"small{i}", parent=parent)
        with django_assert_max_num_queries(3):
            selectors.get_location_tree(selectors.get_locations_for_organization(org_a))

        for i in range(80):
            parent = make_location(org_a, f"big{i}", parent=parent if i % 3 else None)
        with django_assert_max_num_queries(3):
            selectors.get_location_tree(selectors.get_locations_for_organization(org_a))

    def test_ancestors_cost_one_query_regardless_of_depth(self, org_a, make_location, django_assert_num_queries):
        parent = None
        for i in range(40):
            parent = make_location(org_a, f"n{i}", parent=parent)
        with django_assert_num_queries(1):
            assert len(selectors.get_ancestors(parent)) == 39

    def test_descendants_cost_one_query_regardless_of_depth(self, org_a, make_location, django_assert_num_queries):
        root = parent = make_location(org_a, "root")
        for i in range(40):
            parent = make_location(org_a, f"n{i}", parent=parent)
        with django_assert_num_queries(1):
            assert len(list(selectors.get_descendants(root))) == 40
