# 0019. Frontend conventions that work under the strict CSP

- **Status:** Accepted
- **Date:** 2026-10-15
- **Extends:** [0004](0004-alpine-csp-build-self-hosted-js.md)

## Context

ADR 0004 chose the Alpine **CSP build**. Phase 0's draft templates used inline
expressions (`x-data="{ open: false }"`, `@click="open = true"`); testing in a real
browser showed the CSP build rejects **all** of them (object literals, assignment,
`n++`, `!flag`) and does not support `x-model`. The pages silently did nothing.

## Decision

- All client-side state lives in **named `Alpine.data` components**
  (`static/js/components.js`: `disclosure`, `confirmDialog`, `treeControls`, `picker`).
  Templates reference properties, getters and methods by plain name only
  (`x-show="open"`, `@click="toggle"`, `:disabled="blocked"`). No `x-model`.
- `components.js` loads **before** `alpine.min.js` so its `alpine:init` listener exists
  in time.
- Prefer the platform: expand/collapse uses native `<details>`, confirmations use the
  native `<dialog>` (focus trap, Escape, backdrop for free), selection uses a combobox
  with ARIA roles.
- No inline `<script>`, `style=""` or `on*=` handlers. Brand colours are applied via
  the CSSOM (`data-swatch`), which the CSP allows.
- HTMX is configured for the CSP (`includeIndicatorStyles`, `allowEval`,
  `allowScriptTags` off; same-origin only). Every HTMX region has an `aria-live`
  panel and a `[data-htmx-error]` alert shown on failure.
- Forms are rendered with crispy-forms + crispy-tailwind, whose field templates are
  **overridden** in `templates/tailwind/` with the design-system classes (the pack's
  own utilities are not in the compiled stylesheet). `DesignSystemFormMixin` supplies
  widget classes and `aria-invalid` / `aria-describedby`.

## Consequences

- `tests/test_ui_quality.py` fails any template that uses an inline expression, an
  unregistered component, `x-model`, inline script/style/handlers, or an unnamed control.
- Deep trees render without template recursion (see ADR 0010); the browser's own
  nesting limit (~500 elements) makes ~150 levels the practical UI ceiling, while the
  model, API and list view have none.
