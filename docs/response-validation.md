# Response validation

One service layer validates answers for the HTML workspace and the REST API; client-side checks are for usability only.

## The answer contract

A response row stores the **serialized normalized value** produced by the Phase 5 validator for the item's answer type
(`ChecklistResponse.value`, JSON), never raw client input. The stored value re-validates unchanged, so the same rows can be
re-evaluated at any time.

| Answer type | Stored value | Notes |
|---|---|---|
| boolean | `true` / `false` | `False` is an answer |
| yes_no / yes_no_na | `"yes"`, `"no"`, `"na"` | N/A is a real option of `yes_no_na` |
| single_choice | option value | only the item's own active options (and those the rules leave available) |
| multiple_choice | list of option values | min/max selections enforced |
| score | integer | range and step enforced |
| numeric | decimal as a string | decimals, min, max, unit |
| text / long_text | string | length limits, no control characters |
| date / datetime | ISO string | |
| matrix | `{row: column}` or `{row: [columns]}` | rows/columns must exist in the configuration |
| photo / file / signature | - | **refused** until evidence storage exists |

## Where validation runs

`save_answers` builds the candidate answer set (stored answers + the submitted ones) and runs the Phase 6 evaluation, in which the
Phase 5 validators judge each answer against the item's configuration **as tightened by the rules in effect** (option availability,
`SET_VALIDATION_CONSTRAINT`). Results are structured per item: `{item_id: [{"code", "message"}]}`.

Refusals include: unknown / malformed / other-version item ids (one generic `unknown_item`), items the rules currently hide or that
are inactive (`item_hidden`), invalid values, comments over 2000 characters, media answers (`evidence_unavailable`), and
"not applicable" on a required item. A batch is partial: valid current answers are stored, the rest reported, stored data untouched.

Clearing an answer (empty value) deletes the response row including its comment. Submitting an unchanged value changes nothing
(no revision bump, no history).

## Submission checks (`submission_problems`)

For every **visible** item: invalid stored answers; a required item left empty; a required comment missing
(`REQUIRE_COMMENT`, with the rule's prompt); required evidence (`REQUIRE_EVIDENCE`) - blocking while evidence cannot be captured;
and a rule set that cannot be evaluated (cycle). Hidden items never count.

## Evidence boundary

Secure evidence storage belongs to Phase 11. Until then no file reference is ever accepted, so no fake evidence rows or arbitrary
paths can exist; optional media items may stay empty, required ones (or rule-required evidence) block submission with a clear
message, and the UI shows an honest notice instead of an upload control.
