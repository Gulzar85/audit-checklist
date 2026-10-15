# 0039. The application shell: sidebar layout with configuration-driven navigation

- **Status:** Accepted
- **Date:** 2026-10-15
- **Extends:** [0019](0019-csp-compatible-frontend-conventions.md), [0028](0028-light-dark-system-appearance.md)

## Context
Phase 0 shipped a single top navigation bar with hard-coded links. The product will grow to a dozen modules;
a top bar cannot hold them, and editing templates per module invites drift.

## Decision
* Authenticated pages use an **application shell** (`layouts/app.html`): a collapsible sidebar, a light top bar,
  breadcrumbs, a page header, content, footer. `base.html` becomes an alias, so every existing page adopts it
  without edits; signed-out pages use `layouts/auth.html`, chrome-free pages `layouts/blank.html`.
* **Navigation is data** (`apps/core/navigation.py`): sections and items with icon, URL name, visibility,
  active patterns and badge. Unbuilt modules are declared without a URL and render nothing - no fake or broken
  links. Visibility is presentation only; hiding is never authorization, every view still enforces access
  (tested: hidden destinations stay forbidden). The context processor does no queries.
* The sidebar mode is `html[data-sidebar]`, rendered by the server from a cookie (no flash), toggled by an
  Alpine `shell` component; on phones the sidebar is an off-canvas drawer (backdrop, Escape, focus moved in).
* The organization switcher lives in the sidebar and posts to the existing server-validated switch endpoint; its
  list is one capped, lazily evaluated query.
* Global search is a command palette (native dialog) calling a real, tenant-scoped HTMX endpoint over the
  domains that exist; new modules add a result group.

## Consequences
* Adding a module to the menu is one configuration entry.
* The top-bar `sidebar-*` tokens now style the sidebar; the top bar uses surface tokens.
* One more cheap query per page (switcher); query budgets were raised accordingly.