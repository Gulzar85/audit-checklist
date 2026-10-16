# Version comparison

Authorized users can compare any two versions of one checklist: what was added, removed, renamed, reordered or changed in
behaviour. Pages: *Version history -> Compare versions* (`/checklists/<id>/compare/?from=1&to=2`) and the API
`GET /api/v1/checklists/{id}/versions/compare/?from=&to=` ([api/checklist-versions.md](api/checklist-versions.md)).
Implementation: `apps/checklists/comparison.py` (pure diff) + `lifecycle.compare_versions` (loads, checks, audits).

## How it works

1. Each version is turned into its **manifest** ([checklist-versioning.md](checklist-versioning.md#manifest-and-integrity)):
   sections -> items -> options plus rules, in definition order, without primary keys, timestamps or authorship.
2. `compare_manifests(old, new)` diffs the two manifests **by stable code** - never by primary key, because a cloned child has
   a new id:

| Level | Matched by | Reported |
|---|---|---|
| Section | `code` (`S-001`) | added, removed, renamed / description / instructions / requiredness changed, reordered |
| Item (question) | `code`, unique across the version | added, removed, moved to another section, changed text / description / help text / instructions / answer type / requiredness / active flag / reference data, **each changed configuration setting** (`config.max: 10 → 8`), reordered inside its section |
| Option | `value` within the item | added, removed, label changed, activated/deactivated, reordered |
| Rule | `code` (`R-001`) | added, removed, renamed, enabled/disabled, **condition changed**, **actions changed** (both described in words: "Require a comment on “Is the fire exit accessible?”"), reordered |

3. The result is a list of `Change` records: `category` (`section|item|option|rule`), `kind`
   (`added|removed|modified|reordered`), `code`, a readable `summary`, an `impact`, and for field changes the `field`,
   `before` and `after`.

### A code match is not "unchanged"
Two questions with the same code are compared field by field: the same code with a different answer type, range, option list or
requiredness is reported as **modified** with exactly what changed. A code that is removed in one version and re-used for a
different question in a later one is reported as modified (new text, new type), not silently merged. Codes identify, they do
not certify equal meaning.

### Impact
Every change is classified so a long diff can be reviewed by what matters:

* `behavior` - changes what is asked, accepted, required, shown or applied (answer type, settings, requiredness, active flag,
  options, reference data, rules);
* `wording` - text only (question text, descriptions, help text, instructions, names, option labels);
* `structure` - additions, removals, moves and ordering.

### Reordering without noise
Order changes are reported for the **fewest** elements: the longest unchanged run keeps its place (longest increasing
subsequence), so moving one section to the front reports one move, not every section after it.

## Properties

* **Read-only**: neither version is altered (tested: manifests, statuses and `updated_at` are identical afterwards).
* **Same template only**: a version of another checklist - or of another organization - is rejected (`foreign_version`, "The
  selected versions do not belong to the same checklist template."; over the API an unknown or foreign id is `404`).
* **Authorization**: `checklist.view`; organization isolation through the authorized selectors.
* **Efficient**: four queries per version; the diff itself is in memory (it scales with the checklist, not with the history).
* **Audited**: each request records `checklist_version.compared` (who, which versions, how many changes).
* Not based on the integrity hash: two versions with different hashes are diffed structurally; the hash is not a user-facing diff.

## UI

The compare page has two version pickers, filters for *Show* (sections / questions / options / rules), *Kind* and *Impact*,
summary badges (total, added, removed, modified, reordered, "change how it behaves"), and the changes grouped by category.
Each change has an icon + text badge (never colour alone) and an expandable *Details* block (`field`, before, after). All
user-entered text is escaped. The layout is responsive (the filter bar wraps; groups are full-width cards).

Example (`v1 → v2` of the demo checklist):

```
Questions
  Modified  Record refrigerator temperature.: setting “max” changed: 10 → 8.           (behavior)
Rules
  Modified  Rule R-001 actions changed: show “Describe any identified issue.”; make required ... →
            ... ; require a comment on “Is the fire exit accessible?”                      (behavior)
```
