# 0022. Semantic design tokens as the only theming vocabulary

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

If components are written against colours (`bg-indigo-600`, `text-red-600`) a theme can only
re-skin them by editing source. If they are written against *roles* (`bg-primary`,
`text-danger-text`) the colours can change underneath them, and future modules (checklists,
findings, workflows, reports) inherit the visual language without knowing it.

## Decision

- Components consume **semantic tokens** only: `primary`, `surface`, `foreground`, `muted`,
  `border`, `focus`, `link`, `success`/`warning`/`danger`/`info`/`neutral`, `sidebar-*`, plus
  shape (`radius-*`, component radii), typography, density and shadows.
- Names describe **roles, never colours** (no `red`, `blue` ...). A test enforces it.
- The registry (`apps/themes/tokens.py`) is the single source of truth: ~50 tokens, each with
  a kind, bounds and default. It drives validation, CSS generation, the editor form, the API
  and the Tailwind defaults - there is no second list to forget.
- Status colours are declared once and *derived* in the browser (`color-mix`) into a tinted
  background, a border and a readable text colour; a readable foreground (white or near-black)
  is derived server-side by contrast. Status is also never colour alone: badges and alerts
  carry an icon and text.
- Lifecycle aliases `pending`, `active`, `inactive` map to `warning`, `success`, `neutral`.
- `tests/test_theme_contract.py` fails any template, Python or JS file that uses a raw palette
  class, an arbitrary hex colour or a `dark:` variant.

## Consequences

- A token costs one registry entry; hundreds of tokens are deliberately not allowed.
- The top navigation bar is driven by the `sidebar-*` tokens; a future sidebar shell reuses them.
