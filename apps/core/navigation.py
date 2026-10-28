"""Configuration-driven application navigation (sidebar, mobile drawer, command palette).

The navigation is *data*: a list of :class:`NavItem` grouped into sections.  Adding a module
to the menu is one entry here - never template edits.  Rules:

* An item whose ``url_name`` is ``None`` or does not reverse is **not rendered**: modules that
  do not exist yet are declared (so the architecture and the section order are fixed) but
  produce no fake link and no broken link.  ``PLANNED`` documents what is intentionally absent.
* ``visible`` decides whether the item is *shown* (permission, staff, module flag).  It is
  presentation only: **hiding a menu item is never authorization**.  Every view behind a link
  still enforces access through its own policy/selector, and a test proves a hidden destination
  stays forbidden.
* Active state is the most specific matching ``active`` pattern against
  ``<namespace>:<url_name>`` (``fnmatch``), so "Locations" and "Location types" do not light up
  together.

Visibility checks stay cheap (the sidebar renders on every page): ``can_see`` reads the cached,
per-request-memoised RBAC grants, so it adds no query once they are warm.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any

from django.http import HttpRequest
from django.urls import NoReverseMatch, reverse

from .policies import is_active_user, is_platform_staff

Visibility = Callable[[Any], bool]


def authenticated(user: Any) -> bool:
    return is_active_user(user)


def staff_only(user: Any) -> bool:
    return is_platform_staff(user)


def can_see(code: str) -> Visibility:
    """Visible to users who hold the RBAC permission ``code`` somewhere (presentation only)."""

    def check(user: Any) -> bool:
        from apps.permissions.authorization import has_permission_anywhere

        return is_active_user(user) and has_permission_anywhere(user, code)

    return check


def has_perm(codename: str) -> Visibility:
    """Visible to platform staff and to users holding the Django permission ``codename``."""

    def check(user: Any) -> bool:
        return is_active_user(user) and (is_platform_staff(user) or user.has_perm(codename))

    return check


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    icon: str  # Lucide icon name
    section: str
    url_name: str | None = None  # ``None``: declared but not built yet (never rendered)
    url: str | None = None  # a literal path (external/admin links); wins over ``url_name``
    visible: Visibility = authenticated
    active: tuple[str, ...] = ()  # fnmatch patterns on "<namespace>:<url_name>"
    badge: Callable[[HttpRequest], int | None] | None = None
    external: bool = False  # opens in a new tab


@dataclass(frozen=True)
class NavSection:
    key: str
    label: str


SECTIONS: tuple[NavSection, ...] = (
    NavSection("general", "General"),
    NavSection("checklists", "Checklists"),
    NavSection("quality", "Quality"),
    NavSection("analytics", "Analytics"),
    NavSection("administration", "Administration"),
    NavSection("developer", "Developer"),
)

NAVIGATION: tuple[NavItem, ...] = (
    # --- General ---------------------------------------------------------------------------
    NavItem("dashboard", "Dashboard", "layout-dashboard", "general", url_name="core:home", active=("core:home",)),
    NavItem("my_tasks", "My Tasks", "list-checks", "general"),  # planned
    NavItem("notifications", "Notifications", "bell", "general", url_name="notifications:inbox", active=("notifications:*",)),
    # --- Checklists (planned) ---------------------------------------------------------------
    NavItem("templates", "Checklist Templates", "clipboard-list", "checklists", url_name="checklists:list",
            visible=can_see("checklist.view"), active=("checklists:*",)),
    NavItem("assignments", "Assignments", "user-check", "checklists", url_name="assignments:list",
            visible=can_see("assignment.view"), active=("assignments:*",)),
    NavItem("executions", "My work", "play", "checklists", url_name="executions:list",
              visible=can_see("execution.create"), active=("executions:*",)),
    # --- Quality (planned) ------------------------------------------------------------------
    NavItem("findings", "Findings", "triangle-alert", "quality", url_name="findings:list",
            visible=can_see("finding.view"), active=("findings:*",)),
    NavItem("corrective_actions", "Corrective Actions", "wrench", "quality", url_name="corrective_actions:list",
            visible=can_see("corrective_action.view"), active=("corrective_actions:*",)),
    NavItem("verifications", "Verifications", "badge-check", "quality"),
    NavItem("workflow_tasks", "Workflow tasks", "list-todo", "quality", url_name="workflows:tasks", visible=can_see("workflow.task_view"), active=("workflows:tasks", "workflows:instance*")),
    NavItem("workflows", "Workflows", "git-branch", "quality", url_name="workflows:list", visible=can_see("workflow.view"),
            active=("workflows:list", "workflows:create", "workflows:detail", "workflows:edit", "workflows:version")),
    # --- Analytics (planned) ----------------------------------------------------------------
    NavItem("reports", "Reports", "chart-column", "analytics", url_name="reporting:catalogue", visible=can_see("report.view"), active=("reporting:*",)),
    NavItem("dashboards", "Dashboards", "chart-no-axes-combined", "analytics", url_name="dashboards:index", visible=can_see("dashboard.view"), active=("dashboards:*",)),
    # --- Administration ---------------------------------------------------------------------
    NavItem("organizations", "Organizations", "building-2", "administration",
            url_name="organizations:list", active=("organizations:*",)),
    NavItem("locations", "Locations", "network", "administration",
            url_name="locations:tree", active=("locations:*",)),
    NavItem("location_types", "Location types", "tags", "administration",
            url_name="locations:type_list", active=("locations:type_*",)),
    NavItem("users", "Users", "users", "administration", url_name="access:member_list",
            visible=can_see("user.view"), active=("access:member_*",)),
    NavItem("scheduling", "Scheduling", "calendar-clock", "administration", url_name="jobs:dashboard", visible=can_see("jobs.view"), active=("jobs:*",)),
    NavItem("roles", "Roles & Permissions", "shield-check", "administration", url_name="access:role_list",
            visible=can_see("role.view"), active=("access:role_*",)),
    NavItem("themes", "Themes", "palette", "administration", url_name="themes:list", active=("themes:*",)),
    NavItem("settings", "Settings", "settings", "administration"),  # planned
    # --- Developer / platform staff ---------------------------------------------------------
    NavItem("ui", "UI components", "component", "developer", url_name="core:ui", visible=staff_only, active=("core:ui",)),
    NavItem("admin", "Django admin", "shield", "developer", url="/admin/", visible=staff_only, external=True),
    NavItem("api_docs", "API documentation", "book-open", "developer", url="/api/docs/", visible=staff_only, external=True),
)

#: Modules the product will have but that are not built; they render nothing today.
PLANNED: tuple[str, ...] = tuple(item.key for item in NAVIGATION if item.url_name is None and item.url is None)


@dataclass
class ResolvedItem:
    key: str
    label: str
    icon: str
    href: str
    external: bool
    active: bool = False
    badge: int | None = None


@dataclass
class ResolvedSection:
    key: str
    label: str
    items: list[ResolvedItem] = field(default_factory=list)


def _href(item: NavItem) -> str | None:
    if item.url:
        return item.url
    if item.url_name:
        try:
            return reverse(item.url_name)
        except NoReverseMatch:
            return None
    return None


def _current(request: HttpRequest) -> str:
    match = getattr(request, "resolver_match", None)
    if match is None:
        return ""
    namespace = ":".join(match.namespaces) if match.namespaces else ""
    return f"{namespace}:{match.url_name}" if namespace else (match.url_name or "")


def _specificity(item: NavItem, current: str) -> int:
    """Length of the longest ``active`` pattern matching ``current`` (0 = none)."""
    return max((len(pattern) for pattern in item.active if fnmatch(current, pattern)), default=0)


def build_navigation(request: HttpRequest) -> list[ResolvedSection]:
    """The sections and items the current user is shown (presentation only)."""
    user = getattr(request, "user", None)
    if not is_active_user(user):
        return []
    current = _current(request)
    resolved: dict[str, ResolvedSection] = {s.key: ResolvedSection(s.key, s.label) for s in SECTIONS}
    candidates: list[tuple[NavItem, ResolvedItem]] = []
    for item in NAVIGATION:
        href = _href(item)
        if href is None or not item.visible(user):
            continue
        entry = ResolvedItem(item.key, item.label, item.icon, href, item.external, badge=item.badge(request) if item.badge else None)
        resolved[item.section].items.append(entry)
        candidates.append((item, entry))
    best = max((_specificity(item, current) for item, _ in candidates), default=0)
    if best:
        for item, entry in candidates:
            entry.active = _specificity(item, current) == best
    return [section for section in resolved.values() if section.items]
