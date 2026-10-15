# 0040. Component architecture: partials + template tags, state in Alpine, rules enforced by tests

- **Status:** Accepted
- **Date:** 2026-10-15

## Context
Future phases must build screens without redesigning the UI or copying markup. Django has no component model;
HTMX/Alpine under a strict CSP rule out client-side templating.

## Decision
* Components are **template partials** under `templates/components/<group>/`, each documenting its parameters.
  A `ui` template-tag library (`{% button %}`, `{% status_badge %}`, `{% card %}`, `{% page_header %}`,
  `{% modal %}`, `{% drawer %}`, `{% breadcrumbs %}`, `{% icon %}`) is the ergonomic API; block components use
  Django 6's `simple_block_tag`. Everything is auto-escaped; no tag accepts raw HTML.
* Styling is Tailwind component classes in `tailwind/input.css`, written with **semantic utilities** only, so the
  theme engine restyles everything at runtime.
* Interactive state lives in named Alpine components (CSP build): `shell`, `dropdown`, `tabs`, `toasts`,
  `commandPalette`, `networkStatus`, `formGuard`, `treeNav`. Generic behaviours that need no state (modal
  open/close, tooltips, loading links) are delegated listeners in `app.js` driven by `data-*` attributes. Script
  never builds HTML from strings (`textContent` only).
* HTMX is for partial updates (search, filters, lazy tree branches); normal navigation always works.
* The rules live in `docs/ui-design-system.md` and are enforced by tests: no raw colours, no `dark:`, no inline
  style or script, Alpine expressions limited to names, components registered, accessible names present.
* A developer showcase (`/ui/`, platform staff only) renders every component.

## Consequences
New components follow a five-step recipe (docs). Pages stay thin: layout + components + page content.