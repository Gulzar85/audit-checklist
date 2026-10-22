/* Versioned IndexedDB schema.  Each entry upgrades from version (index) to (index + 1); upgrades only ever ADD stores/indexes or
 * transform records in place - they never delete a store that can hold unsynchronized answers (responses, operations, conflicts).
 * A migration that cannot preserve a record must move it to ``conflicts`` for manual recovery instead of dropping it.
 *
 * Version history
 *   1  meta, packages, responses, operations, conflicts, syncState  (Phase 10)
 *   2  evidence (queued files, Phase 11) - purely additive: no existing record is touched
 */
import { STORES } from "./schema.js";

export const INDEXES = {
  [STORES.packages]: [["byNs", "ns"], ["byAssignment", "assignmentId"]],
  [STORES.responses]: [["byAssignment", "assignmentId"], ["bySyncState", "syncState"], ["byNs", "ns"]],
  [STORES.operations]: [["byAssignment", "assignmentId"], ["byState", "state"], ["byNs", "ns"]],
  [STORES.conflicts]: [["byAssignment", "assignmentId"], ["byNs", "ns"]],
  [STORES.evidence]: [["byAssignment", "assignmentId"], ["byState", "state"], ["byNs", "ns"]],
};

export const KEY_PATHS = {
  [STORES.meta]: "key",
  [STORES.packages]: "key",
  [STORES.responses]: "key",
  [STORES.operations]: "opId",
  [STORES.conflicts]: "opId",
  [STORES.syncState]: "ns",
  [STORES.evidence]: "id",
};

function ensureStore(db, tx, name) {
  const store = db.objectStoreNames.contains(name) ? tx.objectStore(name) : db.createObjectStore(name, { keyPath: KEY_PATHS[name] });
  for (const [indexName, path] of INDEXES[name] || []) {
    if (!store.indexNames.contains(indexName)) {
      store.createIndex(indexName, path, { unique: false });
    }
  }
  return store;
}

export const MIGRATIONS = [
  /* 0 -> 1 */ (db, tx) => {
    Object.values(STORES).filter((name) => name !== STORES.evidence).forEach((name) => ensureStore(db, tx, name));
  },
  /* 1 -> 2 */ (db, tx) => {
    ensureStore(db, tx, STORES.evidence);
  },
];

/** Run every migration from ``oldVersion`` to the latest inside the versionchange transaction. */
export function migrate(db, tx, oldVersion) {
  for (let from = oldVersion; from < MIGRATIONS.length; from += 1) {
    MIGRATIONS[from](db, tx);
  }
}
