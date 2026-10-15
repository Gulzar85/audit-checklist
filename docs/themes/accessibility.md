# Theme engine - accessibility

Standard: **WCAG 2.2 level AA**.

* **1.4.3 Contrast (Minimum)** - normal text >= 4.5:1.
* **1.4.11 Non-text Contrast** - focus indicators and UI boundaries >= 3:1.
* **1.4.1 Use of Color** - status is never conveyed by colour alone.
* **2.4.7 Focus Visible** - one focus indicator, drawn from the `focus` token.
* **2.3.3 / prefers-reduced-motion** - honoured globally.

## The report

The editor (live), the theme page and the API (`/themes/{id}/preview/`) evaluate the **complete,
merged** token set (defaults + system theme + the theme) in **both** modes using the same
arithmetic as the browser (`color-mix` in sRGB; the status tint percentages are shared constants).

| Check | Required | Effect when failing |
| --- | --- | --- |
| Text on page background | 4.5:1 | **blocks publishing** |
| Text on cards and panels | 4.5:1 | **blocks publishing** |
| Button text on primary (normal, hover, pressed) | 4.5:1 | **blocks publishing** |
| Button text on secondary | 4.5:1 | **blocks publishing** |
| Navigation text | 4.5:1 | **blocks publishing** |
| Links on background / cards | 4.5:1 | warning |
| Muted text on cards | 4.5:1 | warning |
| Focus indicator on background | 3:1 | warning |
| Status text on its tinted background (x5) | 4.5:1 | warning |

Mandatory checks are the ones a computer can judge reliably and whose failure makes the product
unusable. Advisory checks warn but never reject an unusual-but-legitimate palette. Activation
re-runs the mandatory checks (the system layer underneath may have changed since publishing).

Status text/foreground colours are *derived*: text on a status-filled button uses white or
near-black, whichever contrasts more with the status colour.

## High contrast

The `high_contrast` preset: black/white text, black (or white, in dark mode) borders, a strongly
coloured focus ring, a larger base size, heavier weights, no shadows, relaxed control padding, a
black navigation bar with a yellow active item. Every check passes in both modes (tested).
Status badges and alerts carry an **icon and a text label**, so colour is never the only signal.

## Components

* Visible keyboard focus everywhere (`:focus-visible` outline from the token).
* Form errors use `aria-invalid` + `aria-describedby`; the live preview region is `aria-live`.
* The appearance menu is a `menu` with `menuitemradio` items and `aria-checked`.
* Colour pickers carry an accessible name including the mode ("Primary (light mode)").
* The editor is keyboard-operable, works at phone width, and every section has a heading.

## Reduced motion

`@media (prefers-reduced-motion: reduce)` removes animation and smooth scrolling for the whole
app. A user-level motion preference is reserved for a later phase (the engine adds no animation
of its own).
