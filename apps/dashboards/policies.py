"""Who may use dashboards.  Dashboards add no data access of their own: every widget reads a Phase 19 report, which applies the owning modules' scope; a widget is only
offered when the user may use that report.  ``dashboard.view`` opens the dashboards, ``dashboard.manage`` edits the organization's shared default layouts."""

from __future__ import annotations

from typing import Any

from apps.permissions.registry import P
from apps.reporting import policies as report_policies
from apps.reporting import registry as report_registry

from . import catalog


def can_view(user: Any, organization: Any) -> bool:
    return report_policies.holds(user, P.DASHBOARD_VIEW, organization) and report_policies.can_report(user, organization)


def can_manage(user: Any, organization: Any) -> bool:
    return can_view(user, organization) and report_policies.holds(user, P.DASHBOARD_MANAGE, organization)


def widget_report(widget: catalog.Widget) -> str:
    if widget.kind == catalog.KPI:
        from . import kpis

        kpi = kpis.get(widget.kpi)
        return kpi.report if kpi else ""
    return widget.report


def can_use_widget(user: Any, organization: Any, widget: catalog.Widget) -> bool:
    report = report_registry.get(widget_report(widget))
    return report is not None and report_policies.can_use_report(user, organization, report)


def visible_widgets(user: Any, organization: Any, dashboard: catalog.Dashboard) -> list[catalog.Widget]:
    return [catalog.WIDGETS[code] for code in dashboard.widgets if can_use_widget(user, organization, catalog.WIDGETS[code])]


def visible_dashboards(user: Any, organization: Any) -> list[catalog.Dashboard]:
    if not can_view(user, organization):
        return []
    return [d for d in catalog.DASHBOARDS.values() if visible_widgets(user, organization, d)]
