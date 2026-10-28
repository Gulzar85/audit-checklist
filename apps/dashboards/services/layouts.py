"""Dashboard layouts: personal and organization-shared, validated on save and on load.  A layout chooses among registered widgets; it never grants access -
unauthorized widgets are dropped when it is loaded, whoever saved it."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction

from apps.core.services import log_activity

from .. import catalog, policies
from ..models import DashboardLayout

MAX_WIDGETS = 40


class LayoutError(Exception):
    def __init__(self, message: str, code: str = "invalid_layout") -> None:
        super().__init__(message)
        self.message, self.code = message, code


def clean(dashboard: catalog.Dashboard, raw: Any) -> list[dict[str, Any]]:
    """Validate a submitted layout: a list of ``{"code", "hidden"}`` naming this dashboard's widgets, each at most once."""
    if not isinstance(raw, list) or len(raw) > MAX_WIDGETS:
        raise LayoutError("The layout must be a list of widgets.")
    seen, out = set(), []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"code", "hidden"}:
            raise LayoutError("Each widget entry has only a code and a hidden flag.")
        code, hidden = item.get("code"), item.get("hidden", False)
        if not isinstance(code, str) or code not in dashboard.widgets:
            raise LayoutError("That widget is not part of this dashboard.", "unknown_widget")
        if not isinstance(hidden, bool):
            raise LayoutError("hidden must be true or false.")
        if code in seen:
            raise LayoutError("A widget can appear only once.")
        seen.add(code)
        out.append({"code": code, "hidden": hidden})
    return out


def _normalized(dashboard: catalog.Dashboard, stored: Any) -> list[dict[str, Any]]:
    """A stored layout made valid again: unknown or removed widgets are dropped and widgets added to the dashboard since are appended (visible)."""
    items = []
    seen = set()
    for item in stored if isinstance(stored, list) else []:
        code = item.get("code") if isinstance(item, dict) else None
        if code in dashboard.widgets and code not in seen:
            seen.add(code)
            items.append({"code": code, "hidden": bool(item.get("hidden", False))})
    return items + [{"code": code, "hidden": False} for code in dashboard.widgets if code not in seen]


def effective(user: Any, organization: Any, dashboard: catalog.Dashboard) -> dict[str, Any]:
    """The layout to show: the user's own, else the organization default, else the built-in order - always filtered to widgets the user may use *now*."""
    personal = DashboardLayout.objects.filter(organization=organization, user=user, dashboard=dashboard.code).first()
    shared = None if personal else DashboardLayout.objects.filter(organization=organization, user__isnull=True, dashboard=dashboard.code).first()
    source = "personal" if personal else "organization" if shared else "default"
    items = _normalized(dashboard, (personal or shared).widgets if (personal or shared) else [])
    allowed = {w.code for w in policies.visible_widgets(user, organization, dashboard)}
    return {"source": source, "widgets": [i for i in items if i["code"] in allowed]}


def save(user: Any, organization: Any, dashboard: catalog.Dashboard, raw: Any, *, shared: bool = False) -> DashboardLayout:
    if not policies.can_view(user, organization) or (shared and not policies.can_manage(user, organization)):
        raise PermissionDenied
    widgets = clean(dashboard, raw)
    owner = None if shared else user
    with transaction.atomic():
        layout = DashboardLayout.objects.select_for_update().filter(organization=organization, user=owner, dashboard=dashboard.code).first() if owner else \
            DashboardLayout.objects.select_for_update().filter(organization=organization, user__isnull=True, dashboard=dashboard.code).first()
        if layout is None:
            layout = DashboardLayout.objects.create(organization=organization, user=owner, dashboard=dashboard.code, widgets=widgets, updated_by=user)
        else:
            layout.widgets, layout.updated_by, layout.revision = widgets, user, layout.revision + 1
            layout.save()
    if shared:
        log_activity(verb="dashboards.shared_layout_saved", organization=organization, actor=user, target=layout, description=f"Default layout of {dashboard.title} changed",
                     metadata={"dashboard": dashboard.code, "revision": layout.revision})
    return layout


def reset(user: Any, organization: Any, dashboard: catalog.Dashboard, *, shared: bool = False) -> None:
    if not policies.can_view(user, organization) or (shared and not policies.can_manage(user, organization)):
        raise PermissionDenied
    qs = DashboardLayout.objects.filter(organization=organization, dashboard=dashboard.code)
    deleted = (qs.filter(user__isnull=True) if shared else qs.filter(user=user)).delete()[0]
    if shared and deleted:
        log_activity(verb="dashboards.shared_layout_reset", organization=organization, actor=user, description=f"Default layout of {dashboard.title} reset", metadata={"dashboard": dashboard.code})
