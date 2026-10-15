# 0034. The account appearance page reuses the theme engine, not a second preference store

- **Status:** Accepted
- **Date:** 2026-10-15
- **Relates to:** [0024](0024-theme-precedence-and-resolution.md) (precedence & resolver), [0028](0028-light-dark-system-appearance.md) (colour mode)

## Context

Phase 1 built a complete Dynamic Theme Engine: theme selection by scope, a colour mode
(light / dark / system), a resolver, and an appearance preference (`UserAppearance`, the
`AppearanceForm`, `set_user_appearance`, the `cp_appearance` cookie). Phase 2 adds an *account*
preferences area, and it could either store the user's colour mode again in its own model/table
or ask the existing engine for it.

Two sources of truth for "light, dark or system" would drift: the navbar's appearance menu and
the account page would disagree, and the resolver would have to pick between them.

## Decision

- The account appearance page (`accounts:appearance`) renders the theme engine's own
  `AppearanceForm` and posts to a `FormView` that calls `apps.themes.services.set_user_appearance`,
  then sets/clears the same `cp_appearance` cookie the navbar menu uses. It reuses the engine's
  constants (`Appearance`, `APPEARANCE_COOKIE*`) instead of restating them.
- The account preferences landing page *displays* the stored mode (read via `UserAppearance`)
  and links to the appearance page; it does not write it.
- No new theme state is introduced by the accounts app: themes, tokens, precedence and the
  colour-mode decision all stay inside `apps/themes` (ADR 0024), so there is one resolver and
  one preference to reason about.
- Because the accounts app owns only the *location* of the control and the engine owns the
  *behaviour*, this is explicitly an implementation reuse, not a new preference domain.

## Consequences

- The account page, the navbar menu and the server-rendered first paint can never disagree:
  they read and write one `UserAppearance` row and one cookie.
- No duplicate migration, model or service; if the engine later adds e.g. a motion preference,
  the account page inherits it for free.
- The accounts app stays free of theme logic (no token or precedence code crosses the app
  boundary), preserving the layering conventions in `docs/architecture.md`.
