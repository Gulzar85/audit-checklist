# Offline execution

## What the user can do

1. Sign in online, open **My work → Offline checklists**, and **Prepare for offline use** for an assignment (or use the *Offline* link on a
   My work row).
2. Go offline (or close the browser), open `/offline/app/`, open the prepared checklist, answer questions, move between sections, see
   progress, add comments, review and submit.
3. When the connection returns the queue is synchronized automatically (and with **Sync now**). The checklist is **submitted only when
   the server accepts the submission**.

## Preparation

`GET /api/v1/offline/assignments/{id}/package/` returns the assignment's metadata and the **exact published version** it is pinned to
(sections, items, options, configuration, rules) plus, if an execution already exists, its saved answers with their revisions. The
package is `Cache-Control: no-store` and contains no credentials or permissions.

The client then: validates the structure (`validatePackage`), writes the package **and** the seeded server answers in one atomic
IndexedDB transaction, reads it back, and only then shows *Ready*. Before that it holds only a `preparing`/`failed` staging record, so an
interrupted or partial download never appears ready. A prepared copy is **immutable**: a newer checklist version is never swapped in
silently, and preparing again never replaces a copy that still has unsynchronized work.

If the checklist uses something the offline evaluator cannot do, the server answers `409 offline_unsupported` with the reasons and the
worklist shows "Online only: …". Policy (`apps/executions/offline/compat.py`):

| Feature | Offline |
|---|---|
| all answer types | yes |
| photo/file/signature, rule-required evidence | yes - Blobs stored in IndexedDB and uploaded by the sync engine ([offline-evidence-sync.md](offline-evidence-sync.md)) |
| operators EQUALS, NOT_EQUALS, IS_EMPTY, IS_NOT_EMPTY, ordered comparisons (numbers, scores, dates), BETWEEN, SELECTED, CONTAINS_ANY/ALL | yes |
| text-contains operators, date-time comparisons | refused |
| actions SHOW/HIDE ITEM/SECTION, REQUIRE_ITEM, MAKE_OPTIONAL, REQUIRE_COMMENT, ADD_INSTRUCTIONS, DISPLAY_MESSAGE | yes |
| REQUIRE_EVIDENCE | yes (evaluator version 2) |
| OPTION_AVAILABILITY, SET_VALIDATION_CONSTRAINT | refused |

## Local evaluation

`rules-engine.js` evaluates the same declarative rule data as the server (three-valued logic, hide beats show, required beats optional,
declared N/A cannot waive a required question). It is held to `docs/rule-conformance-vectors.json` - every supported vector must pass
(`static/js/offline/tests/rules-engine.test.js`). `EVALUATOR_VERSION` is bumped on any behavioural change and a package built for another
version is refused. Local validation (`validators.js`) uses the same answer contract; it gives immediate feedback and is **never**
treated as final. Answers to questions the rules hide are retained locally and ignored, exactly like the server (ADR 0056).

## Local saving

A change is validated locally, written to IndexedDB (`responses` + `operations` + the sequence counter in **one transaction**), and only
then labelled "Saved on this device". A failed write (quota, storage error) throws and stores nothing. Writes are serialized per
store instance, so a slow earlier write cannot overwrite a newer one. Repeated edits of an unsent answer are **coalesced** into one queue
entry (same operation id, latest value); an edit made while that entry is in flight becomes a second entry that depends on the first.
Clearing an answer the server never saw removes it without a round trip.

States shown per answer: *Saved on this device · waiting to synchronize*, *Synchronizing…*, *Synchronized with the server*, *Not
accepted by the server*, *Conflict — needs your decision*.

## Manual verification script

```bash
python manage.py seed_demo_offline --organization <slug>
```

1. Sign in, open **My work → Offline checklists**, prepare the *Daily Safety Checklist* at *Demo Restaurant 3*.
2. DevTools → Network → *Offline* (or stop the server). Reload `/offline/app/` - the shell and the prepared checklist open.
3. Answer questions; reload again - the answers are still there, marked "waiting to synchronize".
4. Go online; the queue synchronizes (or press **Sync now**). Check the server: answers present, one receipt per operation.
5. Repeat step 4's request (DevTools → replay the sync POST) - responses say `duplicate: true`; nothing changes.
6. Change one answer on the server (another user / the admin), change the same answer offline, reconnect → **Conflicts** shows both
   values; choose *Keep the server's answer* or *Use my answer*.
7. Complete all required answers and **Review and submit**: it stays "waiting for the server" until accepted.

## Limitations

Offline evidence is limited by browser storage and is not encrypted at rest; no offline execution *start* of an assignment that was not prepared; Background Sync only on browsers that
support it; the offline workspace is a single page (not the full Phase 9 UI); IndexedDB is not encrypted ([offline-security-and-privacy.md](offline-security-and-privacy.md)).
