# Conditional rules during execution

The server is the source of truth. Every render, save, review and submission evaluates the **exact version's** rules
(`snapshot_for_version` + `evaluate`, Phase 6) against the stored answers. No user-supplied expression is ever evaluated.

| Rule effect | During execution |
|---|---|
| `SHOW_ITEM` / `HIDE_ITEM` / `*_SECTION` | the question or section appears / disappears; hidden items accept no answers |
| `REQUIRE_ITEM` / `MAKE_OPTIONAL` | requiredness changes (progress and submission follow) |
| `REQUIRE_COMMENT` | a comment box opens with the rule's prompt; submission needs it |
| `REQUIRE_EVIDENCE` | blocks submission while evidence cannot be captured (Phase 11) |
| `OPTION_AVAILABILITY` | only the available options are offered and accepted |
| `SET_VALIDATION_CONSTRAINT` | the answer is validated against the tightened limits |
| `ADD_INSTRUCTIONS` / `DISPLAY_MESSAGE` | shown under the question |

## How the page updates

The workspace form autosaves through HTMX when a field changes (`change`, debounced 500 ms, requests queued so a newer save
carries the newer revisions). The response is the server-rendered section plus an out-of-band navigation and progress update, so a
conditional question appears (or disappears) only after the server has evaluated the saved answer. There is no client-side rule
logic, hence no infinite update loops. The save indicator is truthful: it reads "Saved HH:MM:SS" only because the server rendered
it after storing; on a network error or expired session it says the answers were **not** saved and the typed input stays on the page.

## Hidden answers: retention policy

* While drafting, an answer whose item becomes hidden is **kept**, never deleted, and ignored (not required, not counted, not
  validated, not shown).
* A client cannot create or edit an answer for a hidden item.
* At submission the hidden answers are stamped `is_hidden = true`: retained for audit, excluded from the read-only views and from
  future scoring.

## Unanswered dependencies

A condition on an unanswered source is *unknown*, not "No": it does not reveal or require anything until the source is answered.
