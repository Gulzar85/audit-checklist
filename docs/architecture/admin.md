# The admin console (Jazzmin)

Decision: [0020](../decisions/0020-admin-jazzmin-and-csp.md). The admin is the **platform
operator's** console (platform staff only). Day-to-day users work in the application UI;
the two link to each other.

## What operators get

| Area | Highlights |
| ---- | ---------- |
| Navigation | sidebar grouped and ordered (Organizations → Locations → Accounts → Roles & Permissions → Auth → Core), an icon per model, top menu to the application (home, organizations, location tree, API docs), one global search over locations |
| Organizations | tabbed form (General / Lifecycle / Branding & contact / Address / Regional / Timestamps / **Members**), logo + favicon previews, status read-only with **Activate / Deactivate / Suspend** list actions that go through the service layer, members managed inline, import/export, history |
| Locations / types | scoped parent and type choices, cycle-safe parent list, cross-organization assignments rejected, deactivate instead of delete, import/export with all-or-nothing hierarchy validation, history |
| Users | memberships inline; identity/profile/permissions in collapsible sections |
| Audit | every admin save is written to `ActivityLog` (`source=admin`, changed fields), plus per-field `simple_history` |

## Branding and theming

Jazzmin 3.x is built on **AdminLTE 4 / Bootstrap 5.3**. It is themed with CSS variables, not the
AdminLTE 3 colour classes that earlier configuration used (they are absent from the stylesheet).
`static/css/admin-brand.css` maps the application's indigo brand onto `--bs-primary` and friends for
light *and* dark mode (Jazzmin sets `data-bs-theme` before first paint, following the OS preference).
Button roles are remapped so "Add" is primary and "Change" a quiet outline.

## Security posture

* **No third-party requests** - Google Fonts are off; a test scans 20+ admin screens for any external origin.
* **CSP.** `/admin/` gets `admin_policy()` (see `apps/core/middleware.py`): the strict site policy plus
  - `script-src` + SHA-256 hashes of Jazzmin's static inline scripts (`ADMIN_CSP_SCRIPT_HASHES` in settings);
  - `style-src-attr 'unsafe-inline'` (inline style *attributes* only).
  No `'unsafe-inline'` for scripts or `<style>`, no `eval`, no external origins.
* **Upgrades.** Jazzmin is pinned `>=3.0.5,<3.1`. If an upgrade changes an inline script,
  `tests/test_admin_theme.py::test_every_inline_script_is_covered_by_a_hash` fails and prints the new digest;
  review it, update the constant (and its comment), done.
* Framing is denied (`X-Frame-Options: DENY`), so Jazzmin's related-object **popup** is off.
* Deletion is disabled for organizations and locations (deactivate instead); location types are
  deletable only while unused (`PROTECT`).

## Configuration map

| Concern | Where |
| ------- | ----- |
| Jazzmin settings, icons, menus, UI tweaks | `config/settings/base.py` (`JAZZMIN_SETTINGS`, `JAZZMIN_UI_TWEAKS`) |
| Admin CSP hashes / style-attr rule | `config/settings/base.py` (`ADMIN_CSP_*`) |
| CSP application | `apps/core/middleware.py` (`AdminContentSecurityPolicyMiddleware`, `admin_policy`) |
| Brand stylesheet / compatibility shim | `static/css/admin-brand.css`, `static/js/admin-compat.js` |
| Per-model admin | `apps/*/admin.py`; shared audit logging in `apps/core/admin.py` (`ActivityLogAdminMixin`) |

## Adding a model to the admin

1. Register it (`@admin.register`), reuse `ActivityLogAdminMixin` if it is business data.
2. Add its icon to `JAZZMIN_SETTINGS["icons"]` (a test fails until you do) and, if its app is new,
   an app icon and an `order_with_respect_to` entry.
3. Prefer `autocomplete_fields` and scoped `ModelForm`s over raw selects for large relations.

## Verifying

```powershell
.\venv\Scripts\python -m pytest tests/test_admin.py tests/test_admin_theme.py
```

To see the real thing under the enforced policy, run the server with `config.settings.test`
(`runserver --insecure`) against a scratch database and open `/admin/` with the browser console
visible: there should be no output.
