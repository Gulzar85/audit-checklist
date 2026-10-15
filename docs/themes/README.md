# Theme engine documentation

| Document | Contents |
| --- | --- |
| [architecture](architecture.md) | Overview, modules, data model, lifecycle, audit, caching, PWA |
| [design-tokens](design-tokens.md) | Token structure, naming, catalogue, value rules, presets |
| [theme-resolution](theme-resolution.md) | Layer and appearance precedence, organization choice, preview |
| [security](security.md) | Threat model, controls, CSP, tests |
| [accessibility](accessibility.md) | WCAG 2.2 AA, the contrast report, high contrast, reduced motion |
| [frontend-integration](frontend-integration.md) | The contract between Django, CSS and templates |

Decisions: ADRs [0021](../decisions/0021-css-variables-over-generated-css.md)-[0028](../decisions/0028-light-dark-system-appearance.md).
API: `/api/v1/themes/`, `/api/v1/themes/active/`, `/api/v1/preferences/appearance/` (see the OpenAPI
schema at `/api/docs/`).
