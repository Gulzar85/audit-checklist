# 0004. Alpine.js CSP build + self-hosted JS

- **Status:** Accepted
- **Date:** 2026-10-14

## Context

A strict CSP without `'unsafe-eval'` blocks Alpine.js's default expression
evaluation and rules out CDN-hosted scripts.

## Decision

Use the **Alpine.js CSP build** (`@alpinejs/csp`) and **self-host** all
third-party JavaScript (HTMX, Alpine, Lucide) under `static/vendor/`.

## Consequences

- No `'unsafe-eval'` or external origins are needed in the CSP.
- Assets are available offline and controlled by our own caching.
- Alpine components must use the CSP-compatible expression subset.
