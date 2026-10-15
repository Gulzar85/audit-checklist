# 0020. The admin: Jazzmin configured for AdminLTE 4, under a narrowly-widened CSP

- **Status:** Accepted
- **Date:** 2026-10-15
- **Extends:** [0004](0004-alpine-csp-build-self-hosted-js.md), [0019](0019-csp-compatible-frontend-conventions.md)

## Context

Phase 0 installed Jazzmin and only set its branding text. Auditing it against the real
product (and against the strict production CSP) found:

| Finding | Consequence |
| ------- | ----------- |
| `use_google_fonts_cdn` left at its default (`True`) | every admin page requested fonts from Google: CSP violations, a privacy leak, no offline posture |
| `site_logo` left at the stock **AdminLTE** logo (also the favicon and login logo) | the admin looked like a different product |
| `JAZZMIN_UI_TWEAKS` used AdminLTE **3** colour classes (`accent-primary`, `sidebar-dark-primary`, `navbar-light`) | Jazzmin 3.x bundles AdminLTE **4** (Bootstrap 5.3): those classes do not exist, the tweaks silently did nothing |
| Jazzmin renders four small **inline scripts** and a few `style=""` attributes | blocked by the enforced production CSP (dev is report-only, so it went unnoticed) |
| Most models had no icon; no sidebar ordering; no route back to the application UI | not a navigable, enterprise-grade console |
| `user_avatar` falls back to a stock *photograph* | unacceptable default |

## Decision

1. **No third-party requests.** `use_google_fonts_cdn=False`; all assets are static files;
   the admin font stack matches the application's. A test fails on any external `src`/`href`.
2. **Brand through CSS variables.** `static/css/admin-brand.css` maps the platform's indigo
   brand onto Bootstrap 5.3's variables (light and dark). The dead AdminLTE 3 tweaks were
   removed; only options that exist in AdminLTE 4 remain. One image (`icons/icon-192.png`)
   drives sidebar logo, login logo and favicon.
3. **Navigable product.** Icons for every registered model and app (a test enforces it), a
   fixed sidebar order (Organizations → Locations → Accounts → Roles → Auth → Core), top-menu
   links into the application (home, organizations, location tree, API docs), one global search
   scoped to locations, tabbed change forms, sticky bulk-action bar, brand-consistent
   button roles, and a *members* inline on the organization page.
4. **A CSP only as wide as Jazzmin needs, and no wider.** `/admin/` gets its own policy via
   `AdminContentSecurityPolicyMiddleware`, derived from the strict site policy by
   `admin_policy()`:
   - `script-src` adds the **SHA-256 hashes** of Jazzmin's four static inline scripts
     (`ADMIN_CSP_SCRIPT_HASHES`) - never `'unsafe-inline'`, never `'unsafe-eval'`;
   - `style-src-attr` allows inline style **attributes** only (`style-src`/`style-src-elem`
     stay strict: no inline `<style>`, no external CSS);
   - nothing else changes: no third-party origins, no wildcards.
   It follows the environment's mode (report-only in development, enforced in production/tests).
5. **Drift is caught in CI, not in production.** Jazzmin is pinned (`>=3.0.5,<3.1`).
   `tests/test_admin_theme.py` renders 20+ admin screens and fails if any inline script's hash is
   not allowed (it names the digest to review) *or* if an allowed hash is no longer used.
6. **One upstream bug is worked around, visibly.** Jazzmin's import-page template loads a script
   that needs Django's jQuery namespace; `static/js/admin-compat.js` (a 3-line shim, documented,
   self-hosted) removes the console error. Delete it when Jazzmin fixes the template.

## Consequences

- The admin works under the same enforced CSP as production with a clean browser console
  (verified manually across the admin screens, and structurally in tests).
- Upgrading Jazzmin is a deliberate act: bump the pin, run the tests, review any new digest.
- The relaxation is documented, minimal and tested: scripts by hash, style *attributes* only.
- Not done on purpose: nonce-patching Jazzmin's templates (a copied 400-line template that
  would itself drift), and `related_modal_active` (it renders an iframe, and framing is denied).
