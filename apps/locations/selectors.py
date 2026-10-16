"""Read-only query helpers for the location tree.

All hierarchy reads are centralised here so no view, serializer or template
implements its own recursion.  Ancestor/descendant lookups use a single
PostgreSQL recursive CTE, so a tree of any depth is read in a constant number
of queries - never one query per node.

The recursive CTEs use ``UNION`` (not ``UNION ALL``).  On a well-formed tree the
result is identical, but if corrupt data ever contained a cycle the recursion
would still terminate instead of looping forever.

Location-scoped access (Phase 4) plugs in here: a role scoped to a location
resolves to ``get_descendants(location, include_self=True)``, which is already
a composable ``QuerySet``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.db import connection
from django.db.models import F, Q, QuerySet
from django.db.models.expressions import RawSQL

from apps.core.policies import is_active_user
from apps.organizations.selectors import organizations_visible_to

from .models import Location, LocationType


# ---------------------------------------------------------------------------
# Scoped reads (organization isolation)
# ---------------------------------------------------------------------------
def locations_visible_to(user: Any) -> QuerySet[Location]:
    """Locations ``user`` may view: the organization boundary *and* their role scope (RBAC).

    Web views and API views both start from this queryset, so the same rule
    applies to every channel.  Anonymous/inactive users see nothing.
    """
    from apps.permissions.authorization import get_accessible_locations
    from apps.permissions.registry import P

    if not is_active_user(user):
        return Location.objects.none()
    return get_accessible_locations(user, P.LOCATION_VIEW)


def location_types_visible_to(user: Any) -> QuerySet[LocationType]:
    """Location types in visible organizations, plus platform-defined types."""
    if not is_active_user(user):
        return LocationType.objects.none()
    visible = organizations_visible_to(user)
    return LocationType.objects.filter(Q(organization__in=visible) | Q(organization__isnull=True))


def available_location_types(organization: Any) -> QuerySet[LocationType]:
    """Active location types an organization may assign (its own + platform)."""
    return LocationType.objects.available_to(organization).filter(is_active=True)


def get_locations_for_organization(organization: Any) -> QuerySet[Location]:
    """Every location (active or not) of one organization."""
    return Location.objects.for_organization(organization)


def get_active_locations(organization: Any = None) -> QuerySet[Location]:
    """Active locations, optionally for a single organization."""
    queryset = Location.objects.filter(is_active=True)
    if organization is not None:
        queryset = queryset.for_organization(organization)
    return queryset


def get_root_locations(organization: Any) -> QuerySet[Location]:
    """Top-level locations (no parent) of one organization."""
    return Location.objects.for_organization(organization).roots()


def get_children(location: Any) -> QuerySet[Location]:
    """Direct children of a location."""
    return Location.objects.children_of(location)


# ---------------------------------------------------------------------------
# Recursive hierarchy reads
# ---------------------------------------------------------------------------
def _table() -> str:
    return Location._meta.db_table


def _pks(locations: Iterable[Any]) -> list:
    return [getattr(item, "pk", item) for item in locations]


def _descendants_sql() -> str:
    table = _table()
    return f"""
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM {table} WHERE parent_id = ANY(%s)
            UNION
            SELECT child.id FROM {table} child JOIN subtree s ON child.parent_id = s.id
        )
        SELECT id FROM subtree
    """


def _ancestors_sql() -> str:
    table = _table()
    return f"""
        WITH RECURSIVE chain(id) AS (
            SELECT parent_id FROM {table} WHERE id = ANY(%s) AND parent_id IS NOT NULL
            UNION
            SELECT node.parent_id FROM {table} node JOIN chain c ON node.id = c.id
            WHERE node.parent_id IS NOT NULL
        )
        SELECT id FROM chain
    """


def _fetch_ids(sql: str, ids: list) -> set:
    if not ids:
        return set()
    with connection.cursor() as cursor:
        cursor.execute(sql, [ids])
        return {row[0] for row in cursor.fetchall()}


def get_descendant_ids(roots: Iterable[Any], *, include_roots: bool = False) -> set:
    """Ids of every descendant of the given locations (one recursive query)."""
    ids = _pks(roots)
    result = _fetch_ids(_descendants_sql(), ids)
    if include_roots:
        result |= set(ids)
    return result


def get_ancestor_ids(locations: Iterable[Any]) -> set:
    """Ids of every ancestor of the given locations (one recursive query)."""
    return _fetch_ids(_ancestors_sql(), _pks(locations))


def get_descendants(location: Any, *, include_self: bool = False) -> QuerySet[Location]:
    """A ``QuerySet`` of every descendant of ``location`` (a single SQL statement).

    The recursion runs inside the query as a sub-select, so the result can be
    filtered, ordered, counted or paginated like any other queryset.
    """
    pk = getattr(location, "pk", location)
    queryset = Location.objects.filter(pk__in=RawSQL(_descendants_sql(), [[pk]]))
    if include_self:
        queryset = Location.objects.filter(
            Q(pk__in=RawSQL(_descendants_sql(), [[pk]])) | Q(pk=pk)
        )
    return queryset


def get_ancestors(location: Location) -> list[Location]:
    """Ancestors ordered root → immediate parent (one query)."""
    if location.parent_id is None:
        return []
    by_id = {
        item.pk: item
        for item in Location.objects.filter(pk__in=RawSQL(_ancestors_sql(), [[location.pk]]))
    }
    chain: list[Location] = []
    current = by_id.get(location.parent_id)
    seen: set = set()
    while current is not None and current.pk not in seen:
        chain.append(current)
        seen.add(current.pk)
        current = by_id.get(current.parent_id)
    chain.reverse()
    return chain


# ---------------------------------------------------------------------------
# Integrity audit
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IntegrityProblem:
    """One violated hierarchy rule, described for a human operator."""

    code: str
    location_id: Any
    message: str


def find_integrity_problems(organization: Any = None) -> list[IntegrityProblem]:
    """Audit the hierarchy and report every violated rule.

    Constraints and triggers make these states impossible through normal writes, so
    a non-empty result means data arrived some other way (a restore, a raw SQL
    script, a bulk statement, triggers disabled during a migration).  Checked:

    * a parent in a different organization;
    * a location type from a different organization;
    * a location that is its own parent;
    * circular parent chains;
    * an *active* location beneath an *inactive* parent.
    """
    scope = Location.objects.all()
    if organization is not None:
        scope = scope.for_organization(organization)

    problems: list[IntegrityProblem] = []
    for pk, name in (
        scope.filter(parent__isnull=False)
        .exclude(parent__organization_id=F("organization_id"))
        .values_list("pk", "name")
    ):
        problems.append(
            IntegrityProblem("cross_organization_parent", pk, f"“{name}” has a parent in another organization.")
        )
    for pk, name in (
        scope.filter(location_type__organization__isnull=False)
        .exclude(location_type__organization_id=F("organization_id"))
        .values_list("pk", "name")
    ):
        problems.append(
            IntegrityProblem("cross_organization_type", pk, f"“{name}” uses a location type from another organization.")
        )
    for pk, name in scope.filter(parent_id=F("id")).values_list("pk", "name"):
        problems.append(IntegrityProblem("self_parent", pk, f"“{name}” is its own parent."))
    for pk, name in scope.filter(is_active=True, parent__is_active=False).values_list("pk", "name"):
        problems.append(
            IntegrityProblem("active_under_inactive_parent", pk, f"“{name}” is active beneath an inactive parent.")
        )

    # Cycles: a node is on a cycle if following its parent chain leads back to it.
    parents = dict(Location.objects.values_list("pk", "parent_id"))
    for pk, name in scope.values_list("pk", "name"):
        if _on_cycle(parents, pk):
            problems.append(
                IntegrityProblem("cycle", pk, f"“{name}” is part of a circular hierarchy.")
            )
    return problems


def _on_cycle(parents: dict, node: Any) -> bool:
    """Whether following ``node``'s parent chain returns to ``node`` (bounded)."""
    current = parents.get(node)
    for _ in range(len(parents)):
        if current is None:
            return False
        if current == node:
            return True
        current = parents.get(current)
    return False


# ---------------------------------------------------------------------------
# Tree assembly
# ---------------------------------------------------------------------------
@dataclass(eq=False)
class TreeNode:
    """A location plus its children, ready for templates and serializers."""

    location: Location
    children: list["TreeNode"] = field(default_factory=list)
    #: ``False`` for ancestors pulled in only to give a filtered result context.
    matched: bool = True

    @property
    def child_count(self) -> int:
        return len(self.children)


@dataclass(frozen=True)
class TreeRow:
    """One row of a flattened tree, with the container tags to close before it."""

    node: TreeNode
    depth: int
    closers: tuple[None, ...]  # one entry per container (</ul></details></li>) to close first
    is_parent: bool


def flatten_tree(roots: list[TreeNode]) -> tuple[list[TreeRow], tuple[None, ...]]:
    """Pre-order flatten a forest for template rendering **without recursion**.

    Django template includes recurse once per level and fail (``RecursionError``)
    around a hundred levels deep.  The tree page therefore renders this flat list,
    emitting balanced container tags from ``closers``; the second return value is
    how many containers remain open after the last row.  Depth is unbounded here.
    """
    rows: list[TreeRow] = []
    open_containers = 0
    stack: list[tuple[TreeNode, int]] = [(node, 0) for node in reversed(roots)]
    while stack:
        node, depth = stack.pop()
        closes = max(open_containers - depth, 0)
        is_parent = bool(node.children)
        rows.append(TreeRow(node, depth, (None,) * closes, is_parent))
        open_containers = depth + (1 if is_parent else 0)
        stack.extend((child, depth + 1) for child in reversed(node.children))
    return rows, (None,) * open_containers


def get_location_tree(
    queryset: QuerySet[Location] | None = None,
    *,
    include_ancestors: bool = True,
    within: QuerySet[Location] | None = None,
) -> list[TreeNode]:
    """Assemble a forest from ``queryset`` using a constant number of queries.

    ``queryset`` is whatever the caller is allowed to see, optionally narrowed by
    a ``django-filter`` filterset.  When ``include_ancestors`` is true, ancestors
    of matched rows that the queryset excluded are added (flagged
    ``matched=False``) so a filtered tree still shows where each result lives.
    ``within`` bounds the ancestors that may be added (pass the caller's authorized queryset):
    a user scoped to a region never sees the names of the levels above it, and the topmost
    location they may see becomes a root of *their* tree.
    Returns the root nodes; ordering is by name then code at every level.
    """
    if queryset is None:
        queryset = Location.objects.all()
    matched = list(queryset.select_related("organization", "location_type"))
    nodes = {item.pk: TreeNode(item) for item in matched}

    if include_ancestors and matched:
        missing = get_ancestor_ids(matched) - nodes.keys()
        if missing:
            allowed = Location.objects.filter(pk__in=missing)
            if within is not None:
                allowed = allowed.filter(pk__in=within.values("pk"))
            for item in allowed.select_related(
                "organization", "location_type"
            ):
                nodes[item.pk] = TreeNode(item, matched=False)

    roots: list[TreeNode] = []
    for node in nodes.values():
        parent = nodes.get(node.location.parent_id) if node.location.parent_id else None
        (parent.children if parent else roots).append(node)

    def _key(node: TreeNode) -> tuple[str, str]:
        return (node.location.name.casefold(), node.location.code)

    roots.sort(key=_key)
    for node in nodes.values():
        node.children.sort(key=_key)
    return roots
