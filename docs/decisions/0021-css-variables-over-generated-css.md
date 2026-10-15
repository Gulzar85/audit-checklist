# 0021. Runtime theming with CSS custom properties, not generated stylesheets

- **Status:** Accepted
- **Date:** 2026-10-15
- **Extends:** [0003](0003-tailwind-compiled-locally.md)

## Context

The platform must change its look per organization and per user, at runtime, without a
redeploy. The obvious alternatives are (a) storing or generating a full CSS file per theme
and (b) re-running Tailwind when a theme changes. Both are heavy: (a) puts a stylesheet -
i.e. code - in the database and produces a large payload per theme; (b) needs Node at
runtime, which the architecture forbids.

## Decision

Tailwind is compiled **once**, against CSS custom properties. Utilities such as
`bg-primary` compile to `background-color: var(--color-primary)`. The active theme is
delivered as a tiny stylesheet (`/themes/tokens.css`, about 3 KB) containing **only**
`--custom-property: value;` declarations in three fixed rule blocks (light, dark, system).

- The stylesheet is generated on demand from validated tokens, memoised in Django's cache
  under a content-addressed key, and validated with an `ETag`.
- Only the active configuration is emitted; there is no per-theme file and nothing is stored.
- The built-in defaults are compiled into `app.css` as a fallback, so the app is fully styled
  (and the offline shell is consistent) even if the theme stylesheet is unavailable.
- Switching light/dark/system flips `<html data-theme>`; no reload, no rebuild.

## Consequences

- Changing a theme is a data change that is visible immediately. No Node at runtime.
- `shadow-*` utilities cannot follow a variable (Tailwind inlines the shadow value at build),
  so components set `box-shadow: var(--shadow-*)` themselves.
- The defaults file (`tailwind/theme-defaults.css`) is *generated* from the token registry and
  a test fails if it is stale, so the fallback can never drift.
