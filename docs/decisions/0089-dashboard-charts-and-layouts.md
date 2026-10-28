# 0089. Dashboard charts and layouts

- **Status:** Accepted
- **Date:** 2026-10-28

## Decision
* Charts are **server-rendered inline SVG** (line, bars, donut): no chart library, no client script, compatible with the strict CSP; accessible (`role="img"`, title, text description, a data table), theme-token colors, dash patterns and text labels instead of color alone, zero-based axes, escaped labels.
* Widgets are lazy-loaded independently with HTMX so a slow or failing widget does not block the page.
* Layouts (`DashboardLayout`: organization, user or none for the organization default, dashboard, ordered `{code, hidden}` list) store only registered widget codes; they are validated on save and repaired on load, and filtered by the viewer's current permissions - a layout never grants access. Shared defaults need `dashboard.manage` and are audited. Reordering uses order numbers (no drag-and-drop dependency).

## Consequences
No new frontend dependency; charts are simple but print- and screen-reader-friendly; personalization cannot inject markup or widen access.
