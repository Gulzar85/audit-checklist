# Theme resolution and precedence

`apps.themes.resolution.resolve_theme(request)` is the only place that decides which look a
request gets. Views, templates (context processor `ui_theme`), the stylesheet endpoint, the PWA
manifest and the API all call it. Nothing else - no middleware, no template, no JavaScript -
re-implements it.

## Token layers (lowest to highest)

1. **System defaults** - the built-in tokens; always complete.
2. **Application theme** - the active *system* theme, if any.
3. **Organization theme** - the active theme of the user's organization.
4. **Temporary preview** - a theme the user is trying out in this session.

Layers *merge*: a later layer overrides only the tokens it sets. An organization theme that
sets only the primary colour keeps the system theme's other values.

## Colour mode (separate from the layers)

```
user's explicit choice (light | dark | system)
        -> theme default appearance
        -> system
```

| Situation | Result |
| --- | --- |
| User explicitly chose **Dark**, organization theme prefers Light | **Dark** (the user wins) |
| User chose **System** | follows the browser/OS, even if the theme prefers dark |
| User has no preference | the active theme's `default_appearance` |
| Two users of one organization choose differently | each gets their mode; the theme is unchanged |
| Anonymous visitor | cookie `cp_appearance` if valid, else the system theme's default, else system |
| Signed-in user with a stale cookie | the cookie is ignored; the saved preference wins |

## Which organization?

* Anonymous: none - **no organization query is made**; the system theme applies.
* Signed in: the active membership marked *default*, else the earliest active membership, in an
  organization that is itself `active`. Users in no organization (and platform staff without a
  membership) get the system theme. A user can never receive another organization's theme.

## Temporary preview

`POST /themes/<id>/try/` stores the theme id in the session. On every request the preview is
re-checked against the policy layer (visible to the user, and the user may edit or activate
themes); anything else is dropped. A preview replaces the organization layer (system layer
stays underneath). It is never persisted, affects only that session, and a banner offers
"Exit preview".

## Delivery

* `<html data-theme="light|dark|system">` is rendered by the server on the first byte.
* `<link rel="stylesheet" href="/themes/tokens.css?v=<hash>">` carries the tokens. `v` is a
  content-addressed version: it changes whenever the active theme (or the registry) changes.
* The stylesheet endpoint is `private, no-cache` with an `ETag` and `Vary: Cookie`.

## Cost

Memoised per request. Signed-in: membership, active themes (system + organization in one query),
appearance = **3 queries**; anonymous: **1 query**. Compiled CSS is memoised in Django's cache
under the content-addressed key, so no invalidation exists to get wrong.

## Fallback

If the theme lookup fails (database trouble while rendering an error page) the context
processor falls back to the built-in defaults; the page still renders.
