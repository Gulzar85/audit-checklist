# 0026. No arbitrary custom CSS, HTML or JavaScript, ever

- **Status:** Accepted
- **Date:** 2026-10-15
- **Related:** [0019](0019-csp-compatible-frontend-conventions.md), [0020](0020-admin-jazzmin-and-csp.md)

## Context

A theme engine that accepts "custom CSS" is a stored-XSS and data-exfiltration feature: CSS can
load remote resources, hide or reposition UI (clickjacking-style attacks) and leak attribute
values; HTML and JS are worse. Administrators of one tenant must not be able to attack the
users of that tenant or of the platform.

## Decision

The engine controls **design tokens, not code**:

- There is **no** `custom_css`, `custom_js` or `html_template` field - on the model, the form,
  the API, the admin or the import format. A test asserts the model has none and that every
  attempt to smuggle such a key in is rejected.
- Tokens are an **allow-list**; unknown keys are errors (not ignored).
- The stylesheet generator can only emit `--custom-property: value;` declarations inside three
  hard-coded rule blocks. Values are re-serialised or looked up, so no user text reaches a
  selector, property name, at-rule or URL. No `@import`, no `url()`, no remote fonts.
- The live preview applies server-computed variables through the CSSOM with a property-name
  allow-list (`^--[a-z0-9-]+$`) - no `innerHTML`, no `eval`.
- Theme export contains validated configuration only (no ids, users, organizations or secrets).

## Consequences

- Organizations get consistent branding without a path to arbitrary markup or styling.
- Anything not expressible as tokens needs a new registry entry (a code change), which is the
  point: the platform stays in control of what a theme can do.
