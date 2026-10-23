# Rule actions

An action is a **declarative effect** on a target when its rule's condition is TRUE. There is no action that runs code, queries
data or changes a stored answer: the engine only computes an *effective state* (see
[conditional-rules.md](conditional-rules.md#evaluation)) that the caller enforces. Actions are data in a registry
(`apps/checklists/rules/actions.py`).

```json
{"type": "REQUIRE_EVIDENCE", "target": {"type": "item", "code": "FIRE-PHOTO"}, "kinds": ["photo"], "min_count": 2}
```

`type` selects the action; `target` is `{"type": "item"|"section", "code": "..."}` (omitted for a checklist-wide message);
the remaining keys are the action's parameters. Unknown parameters are errors.

## The actions

| Action | Target | Effect | Parameters | Graph edge* |
|---|---|---|---|---|
| `SHOW_ITEM` | question | Visible only while a matched show-rule exists (hidden otherwise) | - | yes |
| `HIDE_ITEM` | question | Hidden while matched | - | yes |
| `SHOW_SECTION` | section | The section (and so its questions) is visible only while matched | - | yes |
| `HIDE_SECTION` | section | Hidden while matched | - | yes |
| `REQUIRE_ITEM` | question | Must be answered (while visible) | - | no |
| `MAKE_OPTIONAL` | question | Need not be answered | - | no |
| `DISPLAY_MESSAGE` | question, section or none | An info / warning / error message | `severity` (`info`\|`warning`\|`error`, default `info`), `message` (required, <= 300 chars) | no |
| `REQUIRE_COMMENT` | question | A comment must accompany the answer | `prompt` (optional, <= 200) | no |
| `REQUIRE_EVIDENCE` | question | Evidence must accompany the answer (a *declaration*: files are the evidence phase's) | `kinds` (`photo`,`file`,`signature`; default photo), `min_count` (1-10) | no |
| `SET_VALIDATION_CONSTRAINT` | question | Tighten the answer-type limits | `constraints` (object; keys are the type's `rule_constraints`) | yes |
| `OPTION_AVAILABILITY` | single/multiple-choice question | Restrict which of the question's options may be chosen | `mode` (`only`\|`exclude`), `options` (option values) | yes |
| `ADD_INSTRUCTIONS` | question | Extra instruction text | `text` (required, <= 500) | no |
| `RAISE_FINDING` | question | When matched **and the execution is submitted**, the server records a finding about the question (Phase 12; see [finding-detection.md](finding-detection.md)) | `category`, `severity` (organization codes, required), `title`, `description` (optional; placeholders `{question}` `{answer}` `{location}` `{checklist}`), `requires_review` (bool, default false), `inherit_evidence` (bool, default true) | no |

\* A *graph edge* action changes whether another rule's **source answer** counts (a hidden answer is ignored; an answer
outside the options or limits becomes invalid), so it takes part in cycle detection and decides evaluation order. The others
only annotate the result.

### Show versus hide
A target that has any `SHOW_*` rule is **hidden by default** and shown while at least one show-rule matches. `HIDE_*` hides
while matched and **always wins** over show. Hiding a section hides its questions. A hidden question's answer is ignored (not
validated, not counted for completion) but kept.

### Requirement
`REQUIRE_ITEM` / `MAKE_OPTIONAL` override the question's configured `is_required`; if both match, **required wins**. A hidden
question is never required.

### Constraints (`SET_VALIDATION_CONSTRAINT`)
The allowed keys come from the target's answer type (`rule_constraints`):

| Answer type | Keys |
|---|---|
| `numeric`, `score` | `min`, `max` |
| `text`, `long_text` | `min_length`, `max_length` |
| `date` | `min_date`, `max_date` |
| `datetime` | `min_datetime`, `max_datetime` |
| `multiple_choice` | `min_select`, `max_select` |
| `photo`, `file` | `max_files` |

Values are parsed by the answer type's own configuration schema, and a rule may only **tighten**: a `max` above the
question's own `max` (or a `min` below its `min`) is rejected, and the merged configuration must still be valid. When several
matched rules constrain the same key the tightest wins; an impossible combination applies none of them and reports
`constraint_conflict`.

### Option availability
`only` keeps just the listed options; `exclude` removes them. A rule that would leave no active option is rejected; at run
time matched restrictions are intersected and, if that empties the list, all options stay available and `options_exhausted`
is reported. An answer outside the available options is `invalid`.

### Messages
`DISPLAY_MESSAGE` can target a question (shown with it), a section, or nothing (checklist-wide). Messages are plain text -
clients must escape them (the builder does).

## Conflict summary

| Property | Resolution |
|---|---|
| visibility | hide beats show |
| requirement | required beats optional |
| options | intersection |
| constraints | tightest wins; impossible = none applied + error |
| messages / instructions | accumulate in rule order |
| comment / evidence | any matched rule requires; kinds unioned, largest `min_count` |

## Adding an action
Add an `ActionDef` (code, label, target kind, property, edge?, parameter cleaner) to the registry and handle its effect in
`engine.evaluate`; document it here and add conformance vectors. Describing it for people (`describe.py`) and offering it in
the builder (`builder.ACTION_GROUPS` / the editor template) are the remaining touch points.
