# Frontend integration contract

How Django, CSS and templates cooperate. Future modules (checklists, executions, findings,
corrective actions, workflows, scoring, reports) build UI **only** with these semantic classes
and inherit the visual language automatically.

```
Backend:  Theme -> validated tokens -> /themes/tokens.css (CSS custom properties)
Frontend: Tailwind semantic utilities -> reusable components -> pages
```

## Rules for templates

1. Use semantic utilities, never palette classes or hex colours:

   | Need | Use |
   | --- | --- |
   | Page / card / raised surface | `bg-background` / `bg-surface` / `bg-surface-elevated` |
   | Body / secondary text | `text-foreground` / `text-muted-foreground` |
   | Hover or subtle fill | `bg-muted` |
   | Border or divider | `border-border`, `divide-border` |
   | Brand action | `bg-primary text-primary-foreground` (or `.btn-primary`) |
   | Link | `text-link` (bare `<a>` is already themed) |
   | Status fill / tint / text / border | `bg-success`, `bg-success-soft`, `text-success-text`, `border-success-border` (also `warning`, `danger`, `info`, `neutral`) |
   | Navigation | `bg-sidebar-background`, `text-sidebar-foreground`, `text-sidebar-active`, `bg-sidebar-hover`, `border-sidebar-border` |

2. Do not write `dark:` variants - tokens already switch with the mode.
3. Prefer the component classes (`.btn`, `.card`, `.input`, `.table`, `.badge`, `.alert`,
   `.modal-dialog`, `.menu-panel`, `.pagination`, `.breadcrumbs`, `.empty-state`) over rebuilding
   them. They already use the shape, spacing and shadow tokens.
4. Never use inline `style=` or `<style>`; the CSP forbids both and a test enforces it.
5. Status must not rely on colour alone: pair the badge/alert with an icon and a label
   (see `components/status_badge.html`).
6. Tailwind `shadow-*` utilities are inlined at build time; use a component class or
   `box-shadow: var(--shadow-md)` for theme-aware shadows.

`tests/test_theme_contract.py` fails the build if a template, Python or JS file uses a raw palette
class, an arbitrary hex colour, a `dark:` variant or an inline style.

## Document contract

```html
<html lang="en" data-theme="light|dark|system">      <!-- rendered by the server -->
  <meta name="theme-color" ... data-scheme="light"> <!-- + a dark twin; media follows the mode -->
  <link rel="stylesheet" href="/static/css/app.css">          <!-- Tailwind + defaults -->
  <link rel="stylesheet" href="/themes/tokens.css?v=...">     <!-- the active theme -->
```

* Context: `ui_theme` (a `ResolvedTheme`: `mode`, `tokens`, `source`, `css_url`, `pwa`, ...). Never
  query themes in a template.
* Switching mode: set `data-theme` (the `appearance` Alpine component does this and persists it).
* The preview/gallery uses the same components: `templates/themes/_components_sample.html` and the
  `/themes/gallery/` page.

## CSS variable catalogue

See [design-tokens](design-tokens.md). Colours `--color-*` (light in `:root`, dark in
`[data-theme="dark"]`, and under `@media (prefers-color-scheme: dark)` for `data-theme="system"`),
shape `--radius-*`, `--*-radius`, shadows `--shadow-*`, type `--font-*`, `--line-height`, density
`--spacing-scale` (drives Tailwind's `--spacing`), `--control-padding-y`.

## Live preview mechanics (editor)

`hx-post` on the editor form -> server validates unsaved values -> fragment with
`data-preview-vars` (a dictionary of custom properties) and the accessibility report ->
the `themePreview` Alpine component applies the dictionary to the preview frame with
`element.style.setProperty` (CSSOM, CSP-safe, property names allow-listed) and sets the frame's
`data-theme`. All token logic stays on the server.

## Build

```
python manage.py render_theme_defaults   # only after editing apps/themes/tokens.py
npm run css:build                        # Tailwind (dev dependency only; not a runtime dependency)
```
