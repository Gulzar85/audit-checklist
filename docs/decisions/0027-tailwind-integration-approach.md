# 0027. Tailwind integration: semantic utilities backed by theme variables

- **Status:** Accepted
- **Date:** 2026-10-15
- **Extends:** [0003](0003-tailwind-compiled-locally.md)

## Context

Tailwind v4 is compiled locally; we need `bg-primary` to follow the active theme without
rebuilding, and existing screens must keep working.

## Decision

- `tailwind/theme-defaults.css` (**generated** by `manage.py render_theme_defaults` from the
  token registry) declares every token inside `@theme static`, so Tailwind emits all variables
  and generates semantic utilities (`bg-primary`, `text-foreground`, `border-border`,
  `bg-success-soft`, `text-danger-text`, `bg-sidebar-background` ...). Dark and system values
  are base-layer rules on `[data-theme="dark"]` / `[data-theme="system"]`.
- Tailwind's own scales are rewired to tokens in `input.css`: `--font-sans`, `--text-*`,
  `--radius-*`, and `--spacing` (`calc(0.25rem * var(--spacing-scale))`), so existing
  `rounded-lg`, `text-sm`, `p-4` follow the theme's radius, type and density.
- Component classes (`.btn`, `.card`, `.input`, `.table`, `.badge`, `.alert`, `.modal-dialog`,
  `.nav-link` ...) are written against tokens only; per-component shape and shadow come from
  `--button-radius`, `--card-radius`, `--shadow-*` etc.
- Dark mode needs no `dark:` utilities: tokens switch. The `dark:` variant still exists and
  follows `data-theme`.
- The runtime theme stylesheet is unlayered and loaded after `app.css`, so it overrides the
  defaults. The preview frame sets inline custom properties (CSSOM) on a `data-theme` element.
- Templates were migrated mechanically from raw palette classes; the contract test keeps them
  clean.

## Consequences

- Changing a token's default is: edit the registry, run `render_theme_defaults`, run
  `npm run css:build`. A test fails if either step is forgotten.
- Only utilities that are actually used are emitted; unused semantic names cost nothing.
