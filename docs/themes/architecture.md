# Theme engine - architecture

The theme engine is a **platform-level design system**, not a colour picker. Administrators
configure *design tokens*; the application turns them into CSS variables at runtime; Tailwind
utilities and the reusable components consume the variables. No source change, no redeploy, no
Node at runtime, and no arbitrary CSS or JavaScript.

```
Theme configuration  (apps.themes.models.Theme.tokens)
        |  validated against the token registry
        v
Validated design tokens  (apps/themes/tokens.py)
        |  generated on demand, only the active configuration
        v
CSS custom properties  (GET /themes/tokens.css)
        |  consumed by
        v
Tailwind semantic utilities  (bg-primary, text-foreground, border-border ...)
        v
Reusable UI components  (.btn, .card, .table, .badge, .alert, .input, .nav-link ...)
        v
The entire application
```

## Module layout (`apps/themes/`)

| File | Responsibility |
| --- | --- |
| `tokens.py` | The closed token registry: kinds, bounds, defaults, validation, CSS emission |
| `css.py` | Turns a complete token set into the runtime stylesheet (custom properties only) |
| `contrast.py` | WCAG contrast maths and the accessibility report |
| `presets.py` | Built-in presets (data in the token vocabulary) |
| `models.py` | `Theme`, `ThemeActivation` (ledger), `UserAppearance` |
| `selectors.py` | Read-only queries; `themes_visible_to` is the tenant choke point |
| `policies.py` | Who may view / edit / publish / activate / roll back / preview |
| `services.py` | Every write: create, update, publish, activate, rollback, archive, clone, version, import/export, appearance |
| `resolution.py` | `resolve_theme(request)` - the single place that decides the look |
| `forms.py`, `views.py`, `urls.py`, `tables.py`, `filters.py` | Web UI (CBVs, HTMX preview) |
| `serializers.py`, `apps/api/v1/themes.py` | REST API |
| `admin.py` | Jazzmin admin (oversight and lifecycle; tokens are read-only here) |

The usual platform rules apply: reads through selectors (out of scope = 404), authorization
through policies (forbidden = 403), writes through services, history through simple-history and
the `ActivityLog`.

## Data model

* **Theme** - `organization` (null = system), `name`, immutable `code`, `scope`, `status`
  (`draft|published|active|archived`), `version`, `default_appearance`, `tokens`, timestamps and
  actors. At most one `active` theme per organization and one system-wide (partial unique
  constraints). Published and active themes are immutable; a change is a new version.
* **ThemeActivation** - append-only ledger of activation periods ("what was active when").
* **UserAppearance** - a user's explicit light/dark/system choice (no row = inherit).

Ownership levels: *system* and *organization* are themes; the *user* level is a preference.
See ADR [0023](../decisions/0023-theme-ownership-and-lifecycle.md).

## Lifecycle

```
draft --publish--> published --activate--> active --(replaced)--> published
  \                    \
   +------archive-------+--> archived          active --rollback--> previous published theme
```

* Publishing validates every token and blocks on mandatory WCAG failures.
* Activating makes one theme current; the previous one steps down to *published* (never deleted).
* Rollback re-activates the previous theme; archived themes are not restored.
* "New version" creates a draft with the same `code` and `version + 1`; "clone" creates a new
  code. Neither touches the original.

## Audit

`theme.created|updated|published|activated|archived|cloned|versioned|imported|rolled_back` and
`appearance.preference_changed` are written to the `ActivityLog` (who, organization, theme,
when). simple-history keeps every token change so "who changed the primary colour, from what to
what" is answered on the theme page (token-level diff).

## Caching and performance

No new infrastructure. The compiled stylesheet is memoised in Django's cache under a
content-addressed key (hash of the contributing theme ids and `updated_at`), so activation,
publishing and rollback produce new keys and nothing can be stale. Resolution is memoised per
request and costs at most three small queries (anonymous: one). The stylesheet is ~3 KB and is
revalidated with an `ETag`.

## PWA

The manifest's `theme_color` / `background_color` and the `<meta name="theme-color">` tags come
from the resolved tokens (the manifest link uses `crossorigin="use-credentials"`). The service
worker serves the theme stylesheet network-first and the defaults are compiled into `app.css`,
so the offline shell is never unstyled or inconsistent.

## Operating it

```
python manage.py install_theme_presets      # optional: publish the presets as system themes
python manage.py render_theme_defaults      # after editing apps/themes/tokens.py
npm run css:build                           # rebuild Tailwind after either change
```
