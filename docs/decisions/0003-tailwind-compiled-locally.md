# 0003. Tailwind CSS compiled locally (no CDN)

- **Status:** Accepted
- **Date:** 2026-10-14

## Context

The plan initially allowed loading Tailwind from a CDN. The platform enforces a
strict, nonce-based Content-Security-Policy and must work offline.

## Decision

Compile **Tailwind CSS v4 locally** (`tailwind/input.css` → `static/css/app.css`)
with the standalone CLI via npm. Do not use a CDN at runtime.

## Consequences

- CSP can stay strict (no external `script-src`/`style-src` origins).
- Fully offline-capable; the compiled CSS is precached by the service worker.
- Build step required: `npm run css:build` (or `css:watch` in development).
