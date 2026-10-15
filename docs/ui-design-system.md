# UI design system

One coherent visual language for the whole platform. Every future module (checklist templates,
execution, findings, corrective actions, workflow, scoring, reports) is built from the pieces below and
inherits the active theme. Nothing here is page-specific.

Live reference: **`/ui/`** (platform staff only) renders every component with the real partials. Decisions:
ADRs [0039](decisions/0039-application-shell-and-navigation.md),
[0040](decisions/0040-component-architecture-and-ui-rules.md),
[0041](decisions/0041-status-vocabulary-and-responsive-tables.md); theme engine: [docs/themes](themes/README.md).

## Design principles
1. **Clarity over decoration.** Flat surfaces, thin borders, very subtle shadows. No gradients, glass or
   decorative motion.
2. **Semantic, never literal.** Components speak in roles (`primary`, `surface`, `danger`), not colours. The
   theme engine decides what a role looks like (see [design-tokens](themes/design-tokens.md)).
3. **One pattern per job.** One button hierarchy, one card, one status vocabulary, one navigation.
4. **Mobile first, touch friendly.** Every layout works at 375 px; coarse pointers get 44 px targets.
5. **Accessible by default.** WCAG 2.2 AA practices: labels, focus, keyboard, status never by colour alone.
6. **Server-rendered.** Django templates + HTMX for partial updates; Alpine only for small UI state. No SPA.
7. **UI is not authorization.** Hiding a link never protects anything; every view enforces access.

## Layout system
`templates/layouts/` - `app.html` (the authenticated shell), `auth.html` (signed-out pages),
`blank.html` (no chrome). `base.html` extends `app.html`, so existing pages get the shell automatically.

```
sidebar | top bar
        | breadcrumbs -> page header (+actions) -> content
        | footer / status
```

* **Sidebar** - grouped navigation from configuration; desktop expanded or collapsed (icon-only with
  tooltips), phones an off-canvas drawer (hamburger, Escape and backdrop close it). The mode is
  `html[data-sidebar]`, rendered by the server from the `cp_sidebar` cookie: no flash.
* **Top bar** - sidebar toggle; search (command palette), notifications, help, theme menu, network status,
  user menu. Light by design.
* **Page width** - `{% block page_width %}`: default `max-w-6xl`; `page-wide` (tables, execution screens);
  `page-narrow` (forms, account pages).
* **Blocks** a page fills: `title`, `breadcrumbs`, `page_header`, `content`.

### Navigation is configuration
`apps/core/navigation.py` lists `NavItem`s (icon, label, URL name, section, visibility, active patterns,
badge). Modules that do not exist yet are *declared* with no URL: they render nothing (no fake or broken
links) and are listed in `PLANNED`. To add a module to the menu: set its `url_name`. Visibility callables
(`authenticated`, `staff_only`, `has_perm("app.codename")`) are presentation only; the destination view must
still authorize. Active state is the most specific matching pattern, so "Locations" and "Location types"
never light up together.

## Components
Partials live in `templates/components/<group>/`; template tags (`{% load ui %}`) are the ergonomic API.

| Group | Pieces |
| --- | --- |
| Buttons | `{% button "Save" variant="primary" icon="save" type="submit" %}` - primary, secondary, outline, ghost, danger, link, icon-only |
| Badges | `{% status_badge "overdue" %}` (registry in `apps/core/ui.py`), `{% tone_badge "Label" tone="info" %}` |
| Cards | `{% card title=".." icon=".." tone=".." flush=True %}..{% endcard %}`, `cards/stat_card.html` |
| Alerts / toasts | `alerts/alert.html`; toasts: Django messages, `window.cpToast(msg, tone)`, `HX-Trigger: {"toast": {...}}` |
| Forms | crispy fields (design-system overrides), `forms/actions.html` (Cancel + submit with loading), `switch`, `file_upload`, `section`; `formGuard` (double-submit + unsaved-changes warning) |
| Tables | `core/tables/table.html` (django-tables2; `table-stack` = labelled cards on phones), `tables/row_actions.html`, `tables/bulk_bar.html`, `filter_bar.html` |
| Overlays | `{% modal %}`, `{% drawer %}` (native `<dialog>`), `modals/confirm.html`, dropdown (`x-data="dropdown"`), tooltips (`data-tooltip`) |
| Navigation | sidebar, topbar, `{% breadcrumbs %}`, `{% page_header %}`, tabs (link tabs and in-page `x-data="tabs"`), user menu, org switcher, command palette |
| States | `states/empty.html`, `states/loading.html` (skeletons), `states/error.html`, offline banner, network status |
| Data display | `tree/` (generic, lazy), `timeline/`, `progress/` (native `<progress>`, steps), `avatars/` |

### Page header
`{% page_header title=".." subtitle=".." icon=".." status="draft" %}actions{% endpage_header %}` - the page's
only `<h1>`; actions on the right, primary last. Breadcrumbs above it:
`{% breadcrumbs "Home" "core:home" "Organizations" "organizations:list" organization.name %}` (alternating
label/target; a trailing label is the current page; phones show first and last with an ellipsis; use
`ancestors|as_crumbs:"url_name"` for dynamic trails).

### Standard pages
* **List**: breadcrumbs, page header (primary action), filter bar card, table card (empty state when none),
  pagination.
* **Detail**: header with status and actions, overview card, related cards or tabs, activity timeline.
* **Form**: `page-narrow`, grouped sections (`form-section-title`), `forms/actions.html`, `formGuard`.
* **Dashboard**: stat cards, then structured cards (breakdown, activity), real data only.

## Naming conventions
* Component classes are nouns (`card`, `badge`, `toast`, `tree-row`); modifiers are `card-warning`, `btn-sm`.
* Semantic Tailwind utilities only: `bg-surface`, `text-foreground`, `border-border`, `bg-primary`,
  `text-danger-text`, `bg-success-soft`, `bg-sidebar-*`. Raw palette classes, `dark:` variants, arbitrary hex
  colours and inline styles fail the build (`tests/test_theme_contract.py`).
* Alpine components are camelCase and registered in `static/js/components.js` (`shell`, `dropdown`, `tabs`...).
* Data hooks: `data-modal-open`, `data-modal-close`, `data-tooltip`, `data-toast`, `data-loading-link`.

## Theme tokens
Tokens are CSS custom properties generated from validated theme data (`/themes/tokens.css`), consumed by
Tailwind (`@theme static` in `tailwind/theme-defaults.css`). Colours: `primary(-hover/-active/-foreground)`,
`secondary`, `accent`, `background`, `surface(-elevated)`, `foreground`, `muted(-foreground)`, `border`,
`focus`, `link`, status `success|warning|danger|info|neutral` (+ derived `-soft`, `-border`, `-text`,
`-foreground`), `sidebar-*`. Radius `--radius-sm/md/lg` and per-component radii; shadows `--shadow-sm/md/lg`;
type `--font-family`, sizes, weights, `--line-height`; density `--spacing-scale`. Light, dark and system are
`data-theme` values rendered by the server. Never hard-code a colour: add a token (registry change) instead.

## Responsive rules
| Width | Behaviour |
| --- | --- |
| < 640 px | drawer navigation, single column, `table-stack` rows become labelled cards, form actions stick to the bottom, breadcrumbs collapse to first/last, search is an icon |
| 640-1023 px | two-column grids, full table, inline search button |
| >= 1024 px | persistent sidebar (collapsible), top bar toggle collapses it |
| coarse pointer | buttons, menu items and tabs get a 44 px minimum height |

Tables that stay dense scroll inside `.table-wrap overflow-x-auto`; they never overflow the page.

## Accessibility rules
* One `<h1>` per page (the page header), a `main` landmark, a skip link, `lang`, a meaningful `<title>`.
* Every control has an accessible name: icon-only buttons use `icon_only=True` (becomes `aria-label`).
  Icons are decorative (`aria-hidden`).
* Status = icon + text + tone, never colour alone. Required fields show a mark (and `required`).
* Forms: labels bound with `for`, help and errors linked by `aria-describedby`, invalid fields `aria-invalid`.
* Dialogs are native `<dialog>` with `aria-labelledby`; dropdowns use `aria-haspopup`/`aria-expanded` and arrow
  keys; tabs follow the WAI-ARIA tabs pattern; the tree has arrow-key navigation; toasts are live regions.
* Focus is always visible (`--color-focus`); motion is removed by `prefers-reduced-motion`.
* Contrast: the theme editor enforces WCAG AA on every theme ([accessibility](themes/accessibility.md)).

## HTMX conventions
* Use HTMX for partial updates only (search, filters, lazy tree branches, health widget); plain navigation
  and form posts must keep working without it.
* The CSP-safe config is in `base_root.html` (`allowEval`/`allowScriptTags` off). No inline handlers.
* Indicators: `hx-indicator="#id"` with the `htmx-indicator` class; errors surface as a toast or the nearest
  `[data-htmx-error]` region. Server messages to the user: `HX-Trigger: {"toast": {"message": "..", "tone": "success"}}`.
* Lazy tree: `hx-get`, `hx-trigger="toggle once"`, `hx-target="find .tree-children"`.
* Do not use HTMX on forms guarded by `formGuard` (it listens to the native submit).

## Alpine.js conventions
The CSP build cannot evaluate inline expressions: no object literals, no `open = true`, no `!flag`, no
`x-model`. All state lives in named components (`Alpine.data`); templates reference properties, getters and
methods by plain name (`x-show="open"`, `@click="toggle"`, `:aria-expanded="expanded"`). Getters replace
negations. `components.js` loads before Alpine. Tests reject anything else.

## Icon conventions
Lucide only (self-hosted). `{% icon "name" size="sm" %}` or `<i data-lucide="name" class="h-4 w-4"
aria-hidden="true">`. Sizes: `xs` 14, `sm` 16 (inline/buttons), `md` 20 (navigation, headers), `lg` 24, `xl` 32.
Keep one icon per meaning: plus = create, pencil = edit, trash-2 = delete, eye = view, search, bell, settings,
chevron, filter, download, upload, check, x, ellipsis-vertical = row menu.

## Creating a new page
1. **Identify the page type** (list, detail, form, dashboard, report).
2. **Pick the layout** - extend `base.html` (shell) or `layouts/auth.html`; choose `page_width`.
3. **Reuse components** - breadcrumbs, `page_header`, cards, `status_badge`, tables, states.
4. **Add page-specific content only** - no new button, card, badge or navigation style.
5. **Use semantic tokens** - no colours, no inline styles, no `dark:`.
6. **Make it responsive** - single column first; `table-stack` for tables; check 375 px.
7. **Add loading, empty and error states** (`states/*`).
8. **Check accessibility** - one `<h1>`, names, labels, keyboard, focus.
9. **Add tests** - render, structure, permissions; the contract tests lint colours, CSP and Alpine usage.
10. **Verify light, dark and system** (and a themed organization) in the browser.
To show it in the menu, set the module's `url_name` in `apps/core/navigation.py`.

## Adding a component
1. Create `templates/components/<group>/<name>.html` documenting its parameters in a `{% comment %}`.
2. If it wraps content, add a `simple_block_tag` in `apps/core/templatetags/ui.py`; if it is a small unit, an
   `inclusion_tag`/`simple_tag`. Escape everything; never accept raw HTML.
3. Style it in `tailwind/input.css` (`@layer components`) with semantic utilities; run `npm run css:build`.
4. If it needs state, add an `Alpine.data` component in `components.js` (named getters, no expressions).
5. Show it in `pages/ui/showcase.html` and test it in `tests/test_ui_design_system.py`.

## Rules (enforced by tests where possible)
Do not add a new button/card style when one fits; do not hard-code semantic colours; one icon library; no
page-specific navigation; no duplicated component HTML; no arbitrary spacing when the scale suffices; no
colour-only status; mobile usability first; no CSP weakening; no SPA; no new frontend dependency.