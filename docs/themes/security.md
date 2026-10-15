# Theme engine - security

The engine controls **design tokens, not code**. It must never become a CSS, HTML or JavaScript
injection mechanism. See ADR [0025](../decisions/0025-controlled-token-validation.md) and
[0026](../decisions/0026-no-arbitrary-custom-css-or-js.md).

## Threat model

An administrator of one organization (or a compromised account) tries to: run script in other
users' browsers, load remote resources, restyle the UI to deceive users, read or change another
organization's theme, escalate to system scope, or use import/export as a side channel.

## Controls

| Threat | Control |
| --- | --- |
| Script / HTML injection | No field accepts markup. Tokens are an allow-list; values are parsed and re-serialised; the stylesheet has no way to emit text outside `--prop: value;` declarations. |
| CSS injection (`@import`, `url()`, `expression`, selector breakout, `}`) | Colours are re-built from parsed integers; numbers from parsed floats; choices from a fixed table. Anything else is a validation error, in the model, services, API, form and import. |
| Remote fonts / resources | `font_family` is a choice among fixed local stacks. No URL is representable. |
| `custom_css` / `custom_js` / `html_template` | Do not exist (model, form, API, admin, import). Unknown keys are rejected, not ignored. |
| CSP weakening | The theme arrives as a same-origin `<link>`, not an inline `<style>`. No `unsafe-inline`, no `unsafe-eval`, no script. Tests assert the policy is unchanged. |
| Preview as an attack surface | The server validates and returns a dictionary of `--custom-property` values; the client applies it via the CSSOM with a name allow-list (`^--[a-z0-9-]+$`). No `innerHTML`, no `eval`. |
| Cross-tenant access | All reads start from `themes_visible_to`; foreign ids are 404 (indistinguishable from missing). Filter choices and API organization fields only list visible organizations. |
| Privilege escalation | Separate permissions to edit, publish and activate; system scope needs platform staff; ordinary members can only set their own appearance. |
| Tampering with a live theme | Published/active themes are immutable; changes are new versions. |
| Lifecycle races | Scope lock (organization row / advisory lock) + database constraints: one active theme per scope. |
| Data leakage in export | Export is validated configuration only (no ids, users, organizations, tokens, secrets). Import is size-capped (64 KB), strict UTF-8 JSON, format-checked and fully re-validated; it creates a *draft*. |
| Cookie abuse | `cp_appearance` holds one of three enum values and is validated on read; garbage is ignored. It is deliberately readable by the appearance menu. |
| Admin bypass | Admin writes use the services; tokens are read-only in the admin (no raw JSON box); delete is disabled. |

## Mandatory accessibility as a safety control

A theme that makes text unreadable can hide warnings from users. Publishing and activation are
blocked while the *mandatory* WCAG checks fail (see [accessibility](accessibility.md)).

## Tests

`tests/test_theme_security.py` runs 15 hostile colour payloads through the validator, service,
API and editor; checks that no free-form field exists; that the generated stylesheet contains
only custom-property declarations and the four known selectors; CSP strictness; export hygiene;
and tenant isolation. `tests/test_theme_tokens.py`, `_services.py`, `_api.py` and `_views.py`
cover validation, lifecycle and permissions.
