# 0028. Light / dark / system appearance without a flash and without inline script

- **Status:** Accepted
- **Date:** 2026-10-15
- **Supersedes (in part):** the Phase 0 `localStorage` + `.dark` class toggle in `app.js`

## Context

Phase 0 toggled a `.dark` class from JavaScript after load (a flash of the wrong theme) and
stored the choice in `localStorage` (invisible to the server, per-browser only).

## Decision

- The mode is `<html data-theme="light|dark|system">`, **rendered by the server** on the first
  byte from the user's saved choice (signed in) or the `cp_appearance` cookie (anonymous), or
  the theme default. No script has to run for the first paint, so there is no flash - and
  therefore no inline "early init" script, which the CSP would forbid.
- `system` is pure CSS: the stylesheet has dark values under
  `@media (prefers-color-scheme: dark) { [data-theme="system"] { ... } }`. It follows the OS
  live, with no JavaScript. `color-scheme` is set per mode so native controls match.
- The navbar menu (Alpine component `appearance`) applies a change instantly (sets
  `data-theme`, updates the `theme-color` metas and the cookie) and, when signed in, saves it
  with a POST. The cookie is non-HttpOnly by design and contains only the enum value.
- `<meta name="theme-color">` is rendered twice (light/dark) with `media` queries; the manifest
  takes its colours from the resolved tokens (cookie-authenticated manifest request), and the
  service worker serves the theme stylesheet network-first so the offline shell stays
  consistent (the URL carries a content hash and the defaults live in `app.css`).
- `prefers-reduced-motion` is honoured globally; no motion preference is stored yet (reserved).

## Consequences

- Signed-in users' choice follows them across browsers and devices; anonymous visitors keep a
  per-browser choice.
- Stale `localStorage` values from Phase 0 are simply ignored.
