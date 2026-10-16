"""The authorization service: the one place that answers "may this user do this, here?".

Layers (ADR 0042) - each answers exactly one question and none replaces another:

1. *Authentication* - who is this?                      (``apps.accounts``)
2. *Organization membership* - may they enter this org? (``organizations_visible_to``)
3. *Role* - what may they do?                           (``MembershipRole`` -> ``RolePermission``)
4. *Scope* - where may they do it?                      (organization / location / subtree)

Rules:

* **Additive.** A permission is held if *any* active assignment grants it.  There are no deny
  rules, so adding a role can never remove access.
* **Default deny.** No active membership, an inactive organization, an inactive role or an
  unregistered permission code all mean "no".  An unknown code raises instead of silently
  denying, so a typo is found by the first test that touches it.
* **Superusers bypass** (``is_superuser``) - once, here, and documented.  Business code never
  tests ``is_superuser``, ``is_staff`` or a role name.
* **Legacy bridge.** While ``RBAC_LEGACY_MODEL_PERMISSIONS`` is on, a Django model permission
  named by the registry's ``legacy`` field counts as an organization-wide grant inside any
  organization the user may see.  It keeps pre-RBAC administrators working and is removed in
  one setting flip when data has been migrated to roles.

The resolved grants for a ``(user, organization)`` pair are cached (Django cache, key carries a
per-user and a global version so every access change invalidates it - see ``invalidate_*``).
The cache holds only *which roots grant what*; subtree expansion happens per request, so
moving or adding a location is reflected immediately.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q, QuerySet

from . import registry
from .models import MembershipRole
from .selectors import active_role_slugs_for_user

_ORG = MembershipRole.ScopeType.ORGANIZATION
_SUBTREE = MembershipRole.ScopeType.LOCATION_DESCENDANTS


def _is_active_user(user: Any) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False) and getattr(user, "is_active", False))


def has_role(user: Any, role_slug: str, *, organization: Any = None) -> bool:
    """Return ``True`` if the user holds an active role with the given slug."""
    if not _is_active_user(user):
        return False
    return role_slug in active_role_slugs_for_user(user, organization=organization)


# ---------------------------------------------------------------------------
# Cache versions
# ---------------------------------------------------------------------------
def _ttl() -> int:
    return int(getattr(settings, "RBAC_CACHE_TTL", 300))


def _legacy_enabled() -> bool:
    return bool(getattr(settings, "RBAC_LEGACY_MODEL_PERMISSIONS", True))


def _versions(user_id: Any) -> tuple[int, int]:
    return cache.get(f"rbac:uv:{user_id}", 0), cache.get("rbac:gv", 0)


def invalidate_user(user_id: Any) -> None:
    """Drop every cached grant of one user (role assignment, membership or status change)."""
    key = f"rbac:uv:{getattr(user_id, 'pk', user_id)}"
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, None)


def invalidate_all() -> None:
    """Drop every cached grant (a role's permissions or active flag changed)."""
    try:
        cache.incr("rbac:gv")
    except ValueError:
        cache.set("rbac:gv", 1, None)


# ---------------------------------------------------------------------------
# Resolved access for one (user, organization)
# ---------------------------------------------------------------------------
@dataclass
class Access:
    """What a member may do in one organization, and where."""

    organization_id: str
    #: ``code`` held organization-wide.
    org_wide: frozenset[str] = frozenset()
    #: ``code -> [(include_descendants, location_id)]`` for location-scoped grants.
    roots: dict[str, list[tuple[bool, str]]] = field(default_factory=dict)
    superuser: bool = False
    _expanded: dict[str, frozenset[str]] = field(default_factory=dict, repr=False, compare=False)

    @property
    def codes(self) -> set[str]:
        return set(self.org_wide) | set(self.roots)

    def holds(self, code: str) -> bool:
        return self.superuser or code in self.org_wide or code in self.roots

    def is_org_wide(self, code: str) -> bool:
        return self.superuser or code in self.org_wide

    def location_ids(self, code: str) -> frozenset[str]:
        """Every location id ``code`` is held at through location-scoped grants (subtrees expanded)."""
        if code not in self._expanded:
            from apps.locations.selectors import get_descendant_ids

            entries = self.roots.get(code, [])
            ids = {location_id for _, location_id in entries}
            subtree_roots = [location_id for with_children, location_id in entries if with_children]
            if subtree_roots:
                ids |= {str(pk) for pk in get_descendant_ids(subtree_roots)}
            self._expanded[code] = frozenset(ids)
        return self._expanded[code]

    def allows_location(self, code: str, location_id: Any) -> bool:
        return self.is_org_wide(code) or str(location_id) in self.location_ids(code)


def _build_access(user: Any, organization_id: Any) -> Access:
    """Resolve grants from the database (membership state, active roles, role permissions)."""
    from apps.organizations.models import Organization, OrganizationMembership

    empty = Access(str(organization_id))
    if getattr(user, "is_superuser", False):
        return Access(str(organization_id), superuser=True)

    org_wide: set[str] = set()
    roots: dict[str, list[tuple[bool, str]]] = {}

    member = (
        OrganizationMembership.objects.filter(
            user=user, organization_id=organization_id, is_active=True
        ).values_list("pk", "organization__status").first()
    )
    if member is None or (member[1] != Organization.Status.ACTIVE and not getattr(user, "is_staff", False)):
        # Not a member (or the organization is switched off): only the legacy bridge can apply.
        member = None

    if member is not None:
        rows = (
            MembershipRole.objects.filter(
                membership_id=member[0],
                is_active=True,
                role__is_active=True,
                role__role_permissions__permission__is_active=True,
            )
            .filter(Q(role__organization_id=organization_id) | Q(role__organization__isnull=True))
            .values_list("scope_type", "location_id", "role__role_permissions__permission__code")
            .distinct()
        )
        for scope_type, location_id, code in rows:
            if scope_type == _ORG:
                org_wide.add(code)
            else:
                roots.setdefault(code, []).append((scope_type == _SUBTREE, str(location_id)))

    if _legacy_enabled() and getattr(user, "is_authenticated", False):
        visible = member is not None or getattr(user, "is_staff", False)
        if visible:
            for definition in registry.REGISTRY:
                if definition.legacy and user.has_perm(definition.legacy):
                    org_wide.add(definition.code)

    if not org_wide and not roots:
        return empty
    return Access(str(organization_id), frozenset(org_wide), roots)


def get_access(user: Any, organization: Any) -> Access:
    """The (cached) resolved access of ``user`` in ``organization``; empty for anyone not allowed in."""
    if not _is_active_user(user) or organization is None:
        return Access(str(getattr(organization, "pk", organization)))
    organization_id = getattr(organization, "pk", organization)
    versions = _versions(user.pk)
    memo = getattr(user, "_rbac_memo", None)
    if memo is None:
        memo = {}
        user._rbac_memo = memo
    key = (str(organization_id), versions)
    if key in memo:
        return memo[key]

    cache_key = f"rbac:a:{user.pk}:{organization_id}:{versions[0]}:{versions[1]}"
    stored = cache.get(cache_key)
    if stored is None:
        access = _build_access(user, organization_id)
        cache.set(
            cache_key,
            (sorted(access.org_wide), access.roots, access.superuser),
            _ttl(),
        )
    else:
        org_wide, roots, superuser = stored
        access = Access(str(organization_id), frozenset(org_wide), roots, superuser)
    if len(memo) > 32:
        memo.clear()
    memo[key] = access
    return access


def _require_registered(code: str) -> None:
    if not registry.is_registered(code):
        raise ImproperlyConfigured(f"Unknown permission code {code!r}; add it to apps/permissions/registry.py.")


def _organization_id(organization: Any = None, location: Any = None) -> Any:
    if organization is not None:
        return getattr(organization, "pk", organization)
    if location is not None:
        return location.organization_id
    return None


# ---------------------------------------------------------------------------
# The public API
# ---------------------------------------------------------------------------
def has_permission(user: Any, code: str, *, organization: Any = None, location: Any = None) -> bool:
    """May ``user`` perform ``code`` in ``organization`` (and, when given, at ``location``)?

    With only an organization the question is *organization-level*: it needs the permission
    held organization-wide.  With a location it also accepts a grant scoped to that location
    or an ancestor subtree.  With neither, see :func:`has_permission_anywhere`.
    """
    _require_registered(code)
    if not _is_active_user(user):
        return False
    organization_id = _organization_id(organization, location)
    if organization_id is None:
        return has_permission_anywhere(user, code)
    if location is not None and location.organization_id != organization_id:
        return False  # a forged location from another organization
    access = get_access(user, organization_id)
    if location is not None:
        return access.allows_location(code, location.pk)
    return access.is_org_wide(code)


def has_permission_anywhere(user: Any, code: str) -> bool:
    """Whether ``user`` holds ``code`` in at least one organization (any scope).

    Used to decide whether to *offer* an action ("New location"); the action itself is always
    re-checked against a concrete organization/location.
    """
    _require_registered(code)
    if not _is_active_user(user):
        return False
    if user.is_superuser:
        return True
    if _legacy_enabled():
        definition = registry.BY_CODE[code]
        if definition.legacy and user.has_perm(definition.legacy) and _candidate_organization_ids(user):
            return True
    return any(get_access(user, organization_id).holds(code) for organization_id in _candidate_organization_ids(user))


def visible_organization_ids(user: Any) -> list[Any]:
    """Ids of the organizations ``user`` may enter (cached like the grants; see ``invalidate_*``)."""
    from apps.organizations.selectors import organizations_visible_to

    if not _is_active_user(user):
        return []
    versions = _versions(user.pk)
    memo = getattr(user, "_rbac_orgs", None)
    if memo is not None and memo[0] == versions:
        return memo[1]
    key = f"rbac:vo:{user.pk}:{versions[0]}:{versions[1]}"
    ids = cache.get(key)
    if ids is None:
        ids = list(organizations_visible_to(user).values_list("pk", flat=True))
        cache.set(key, ids, _ttl())
    user._rbac_orgs = (versions, ids)
    return ids


_candidate_organization_ids = visible_organization_ids


def can_access_organization(user: Any, organization: Any) -> bool:
    """Layer 2: may ``user`` enter ``organization`` at all (active membership, active org)."""
    from apps.organizations.selectors import organizations_visible_to

    if not _is_active_user(user) or organization is None:
        return False
    return organizations_visible_to(user).filter(pk=getattr(organization, "pk", organization)).exists()


def can_access_location(user: Any, location: Any, code: str = registry.P.LOCATION_VIEW) -> bool:
    """May ``user`` perform ``code`` (default: view) at ``location``?"""
    if location is None:
        return False
    return has_permission(user, code, location=location)


def can_view_location_id(user: Any, organization_id: Any, location_id: Any, code: str = registry.P.LOCATION_VIEW) -> bool:
    """Cheap id-based variant of :func:`can_access_location` (no object needed)."""
    _require_registered(code)
    if not _is_active_user(user) or location_id is None:
        return False
    return get_access(user, organization_id).allows_location(code, location_id)


def get_accessible_organizations(user: Any, code: str | None = None) -> QuerySet:
    """Organizations the user may enter; with ``code``, only those where they hold it (any scope)."""
    from apps.organizations.selectors import organizations_visible_to

    visible = organizations_visible_to(user)
    if code is None:
        return visible
    _require_registered(code)
    allowed = [pk for pk in visible_organization_ids(user) if get_access(user, pk).holds(code)]
    return visible.filter(pk__in=allowed)


def get_accessible_locations(user: Any, code: str = registry.P.LOCATION_VIEW, *, organization: Any = None) -> QuerySet:
    """Locations where ``user`` holds ``code`` - the scope-aware, authorized base queryset.

    Authorize *first*, then filter/search/paginate on top of this.  Organization-wide grants
    contribute every location of the organization; scoped grants contribute their subtrees.
    """
    from apps.locations.models import Location

    _require_registered(code)
    if not _is_active_user(user):
        return Location.objects.none()
    candidates = visible_organization_ids(user)
    if organization is not None:
        wanted = getattr(organization, "pk", organization)
        candidates = [pk for pk in candidates if str(pk) == str(wanted)]
    wide: list[Any] = []
    scoped: list[Any] = []
    for organization_id in candidates:
        access = get_access(user, organization_id)
        if access.is_org_wide(code):
            wide.append(organization_id)
        elif access.holds(code):
            scoped.extend(access.location_ids(code))
    if not wide and not scoped:
        return Location.objects.none()
    return Location.objects.filter(Q(organization_id__in=wide) | Q(pk__in=scoped))


def accessible_descendants(user: Any, location: Any, code: str = registry.P.LOCATION_VIEW) -> QuerySet:
    """The descendants of ``location`` (inclusive) that the user may reach with ``code``."""
    from apps.locations.selectors import get_descendants

    reachable = get_accessible_locations(user, code, organization=location.organization_id)
    return get_descendants(location, include_self=True).filter(pk__in=reachable.values("pk"))


def get_effective_permissions(user: Any, organization: Any) -> dict[str, dict[str, Any]]:
    """``code -> {"organization": bool, "locations": [ids]}`` - what the user may do, and where."""
    access = get_access(user, organization)
    codes = registry.codes() if access.superuser else sorted(access.codes)
    return {
        code: {
            "organization": access.is_org_wide(code),
            "locations": [] if access.is_org_wide(code) else sorted(access.location_ids(code)),
        }
        for code in codes
    }


def permissions_in_scope(user: Any, organization: Any, location: Any = None, codes: Iterable[str] | None = None) -> set[str]:
    """The subset of ``codes`` (default: all) the user holds at that organization/location."""
    wanted = list(codes) if codes is not None else list(registry.codes())
    return {code for code in wanted if has_permission(user, code, organization=organization, location=location)}


# ---------------------------------------------------------------------------
# Domain object checks (placeholders for later phases; stable signatures)
# ---------------------------------------------------------------------------
def can_view_checklist(user: Any, checklist: Any) -> bool:
    """Whether ``user`` may view ``checklist``."""
    return _is_active_user(user)


def can_execute_checklist(user: Any, execution: Any) -> bool:
    """Whether ``user`` may execute / continue ``execution``."""
    return _is_active_user(user)


def can_manage_finding(user: Any, finding: Any) -> bool:
    """Whether ``user`` may manage (raise, edit, close) ``finding``."""
    return _is_active_user(user)


def can_verify_action(user: Any, corrective_action: Any) -> bool:
    """Whether ``user`` may verify a corrective action."""
    return _is_active_user(user)
