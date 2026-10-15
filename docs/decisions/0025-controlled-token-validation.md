# 0025. Structured, validated token storage - not free-form JSON

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

Themes need flexible storage (new tokens must not require a migration per token) that is also
validatable, versionable and safe. One unrestricted `JSONField` would make the database an
arbitrary-content store.

## Decision

`Theme.tokens` is a JSON document **validated against a closed registry** on every write
(`validate_tokens`): shape (`light`/`dark`/`shared` sections only), token names (allow-list),
and each value by kind:

- `color`: `#rgb`, `#rrggbb`, `rgb()`, `hsl()` (opaque only), **re-serialised to `#rrggbb`**
  from parsed integers; named colours, `var()`, `calc()`, alpha, and anything else are rejected;
- `px` / `number`: finite numbers inside per-token ranges;
- `choice`: one key from a fixed table (font stacks, radii, shadows, density, weights); the CSS
  comes from *our* table, never from the stored value.

Validation runs in the model (`clean`), the services, the API serializers, the editor form and
imports; all problems are reported together. The stored value is the canonical form.

Why JSON rather than a column per token: the vocabulary will grow with the product; JSON plus
a registry gives migrations-free additions, cheap diffs for history, and a single definition.
What is *queried* (organization, scope, status, code, version) are real indexed columns.

## Consequences

- Tokens added later are available immediately; themes that predate them inherit the default.
- A document containing an unknown token is rejected (never silently ignored), which is what
  makes import/export trustworthy.
