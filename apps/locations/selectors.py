"""Read-only query helpers for the location tree.

Heavy hierarchy reads are centralised here so no view implements its own
recursion.  Descendant/ancestor lookups use a single PostgreSQL recursive CTE,
so a tree of any depth is fetched in one query (no per-node queries).
"""

from __future__ import annotations

from typing import Any

from django.db import connection
from django.db.models import Q, QuerySet

from apps.organizations.selectors import organizations_visible_to

from .models import Location, LocationType


def locations_for_organization(organization_id: Any) -> QuerySet[Location]:
    """Active locations belonging to an organization."""
    return Location.objects.filter(organization_id=organization_id, is_active=True)


def locations_visible_to(user: Any) -> QuerySet[Location]:
    """Locations in every organization ``user`` may see (org-isolated)."""
    return Location.objects.filter(organization__in=organizations_visible_to(user))


def available_location_types(organization_id: Any) -> QuerySet[LocationType]:
    """Active location types usable by an organization (its own + platform)."""
    return (
        LocationType.objects.filter(is_active=True)
        .filter(Q(organization_id=organization_id) | Q(organization__isnull=True))
        .select_related("organization")
        .order_by("sort_order", "name")
    )


def _table() -> str:
    return Location._meta.db_table


def descendant_ids(location: Any, *, include_self: bool = False) -> set:
    """Return the set of ids in ``location``'s subtree using one recursive CTE."""
    table = _table()
    sql = f"""
        WITH RECURSIVE subtree AS (
            SELECT id FROM {table} WHERE parent_id = %s
            UNION ALL
            SELECT child.id FROM {table} child JOIN subtree s ON child.parent_id = s.id
        )
        SELECT id FROM subtree
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [getattr(location, "pk", location)])
        ids = {row[0] for row in cursor.fetchall()}
    if include_self:
        ids.add(getattr(location, "pk", location))
    return ids


def ancestor_ids(location: Any) -> list:
    """Return ancestor ids nearest-first using one recursive CTE."""
    table = _table()
    sql = f"""
        WITH RECURSIVE chain AS (
            SELECT parent_id AS id FROM {table} WHERE id = %s AND parent_id IS NOT NULL
            UNION ALL
            SELECT node.parent_id FROM {table} node JOIN chain c ON node.id = c.id
            WHERE node.parent_id IS NOT NULL
        )
        SELECT id FROM chain
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [getattr(location, "pk", location)])
        return [row[0] for row in cursor.fetchall()]


def descendants_of(location: Any) -> QuerySet[Location]:
    """QuerySet of every descendant of ``location``."""
    ids = descendant_ids(location)
    return Location.objects.filter(pk__in=ids)


def ancestors_of(location: Any) -> list[Location]:
    """List of ancestors ordered root → immediate parent."""
    ids = ancestor_ids(location)
    if not ids:
        return []
    by_id = {loc.pk: loc for loc in Location.objects.filter(pk__in=ids)}
    return [by_id[pk] for pk in reversed(ids) if pk in by_id]


def location_tree(
    *,
    organization_id: Any = None,
    visible_organization_ids: Any = None,
) -> list[Location]:
    """Assemble the location forest in a single query.

    Returns the root locations; each node carries a ``children_nodes`` list
    attribute for template recursion.  The tree is built in Python from one
    query, so the number of queries does not grow with the tree size.
    """
    queryset = Location.objects.select_related("organization", "location_type", "parent").order_by(
        "organization__name", "name"
    )
    if organization_id is not None:
        queryset = queryset.filter(organization_id=organization_id)
    if visible_organization_ids is not None:
        queryset = queryset.filter(organization_id__in=visible_organization_ids)

    nodes = list(queryset)
    by_id = {node.pk: node for node in nodes}
    for node in nodes:
        node.children_nodes = []

    roots: list[Location] = []
    for node in nodes:
        parent = by_id.get(node.parent_id)
        if parent is not None:
            parent.children_nodes.append(node)
        else:
            roots.append(node)
    return roots
