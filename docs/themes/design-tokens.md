# Design tokens

A theme is a set of **tokens**: validated values for a closed list of semantic names. The
registry in `apps/themes/tokens.py` is the single source of truth - it drives validation, CSS
generation, the editor form, the API and the Tailwind defaults.

## Storage shape

```json
{
  "light":  { "primary": "#4f46e5", "surface": "#ffffff" },
  "dark":   { "primary": "#818cf8", "surface": "#0f172a" },
  "shared": { "font_family": "sans", "radius_md": 8, "density": "comfortable" }
}
```

`light`/`dark` hold colours (per mode); `shared` holds everything else. A theme stores **only the
tokens it overrides**; unset tokens inherit from the layer below (see
[theme-resolution](theme-resolution.md)). Stored values are in canonical form.

## Naming convention

* Token keys are `snake_case` roles: `primary`, `primary_hover`, `surface_elevated`,
  `muted_foreground`, `sidebar_active`, `radius_md`, `button_radius`.
* CSS variables are `--color-<role>` (kebab-case), `--radius-*`, `--shadow-*`, `--font-*`,
  `--spacing-scale`, `--control-padding-y`, `--<component>-radius`.
* Names describe **roles, never colours** (no `red`, `blue`). The set is closed: a new token is a
  registry change, not a data change.

## Catalogue

| Group | Tokens | CSS variables |
| --- | --- | --- |
| Brand colours | `primary`, `primary_hover`, `primary_active`, `primary_foreground`, `secondary`, `secondary_foreground`, `accent`, `link`, `focus` | `--color-primary` ... |
| Surfaces | `background`, `surface`, `surface_elevated`, `foreground`, `muted`, `muted_foreground`, `border` | `--color-background` ... |
| Status | `success`, `warning`, `danger`, `info`, `neutral` | `--color-success` ... plus derived `-foreground`, `-soft`, `-border`, `-text` |
| Navigation | `sidebar_background`, `sidebar_foreground`, `sidebar_active`, `sidebar_hover`, `sidebar_border` | `--color-sidebar-*` |
| Typography | `font_family` (safe stacks), `font_size_base/small/large` (px), `heading_weight`, `body_weight`, `line_height` | `--font-family`, `--font-size-*`, `--heading-weight`, `--body-weight`, `--line-height` |
| Shape | `radius_sm/md/lg` (px) | `--radius-sm/md/lg` (`--radius-full` is fixed) |
| Components | `button_radius`, `input_radius`, `card_radius`, `table_radius`, `modal_radius` (`none/sm/md/lg/full`) | `--button-radius` ... |
| Density | `density` (`compact/comfortable/spacious`), `component_padding` (`compact/normal/relaxed`) | `--spacing-scale`, `--control-padding-y` |
| Shadows | `shadow_sm/md/lg` (`none/subtle/medium/strong`) | `--shadow-sm/md/lg` |

Status lifecycle aliases for future modules: `pending` -> warning, `active` -> success,
`inactive` -> neutral (`.badge-pending`, `.badge-active`, `.badge-inactive`).

## Value rules

| Kind | Accepts | Normalised to |
| --- | --- | --- |
| color | `#rgb`, `#rrggbb`, `rgb(r,g,b)`, `rgb(r g b)`, `hsl(h,s%,l%)` - opaque only | `#rrggbb` (lower case) |
| px | a finite number within the token's range | `rem` in CSS |
| number | a finite number within range | as is |
| choice | one of the fixed keys | our fixed CSS value |

Rejected: named colours, `var()`, `calc()`, `url()`, transparency, hex with alpha, anything with
`;{}<>@\` - and every unknown token name or section.

Ranges (examples): base text 12-20 px, small 10-16, large 14-28, line height 1.2-2.0, radii 0-16 /
0-24 / 0-32 px.

## Presets

`default`, `corporate`, `dark`, `high_contrast`, `minimal` (`apps/themes/presets.py`). A preset is
a dictionary of token overrides plus a default appearance - data, not templates. Add one by
appending to `PRESETS`; a test validates every preset and requires it to pass the mandatory
contrast checks (`high_contrast` must pass *all* checks).

## Adding a token

1. Add a `Token` to `_TOKEN_LIST` (kind, bounds, default for light and dark).
2. `python manage.py render_theme_defaults` and `npm run css:build`.
3. Use the new utility/variable in a component. Existing themes inherit the default.

Tests fail if the generated defaults or the build are stale.
