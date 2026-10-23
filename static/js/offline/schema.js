/* Shared constants and record validators of the offline engine (Phase 10).
 *
 * Everything stored in IndexedDB is validated here before it is written or trusted: the database is user-editable, so a record is
 * only ever a *cache* of something the server said, or a *local intent* waiting to be sent - never an authority.
 */

export const SCHEMA_VERSION = 1; // the sync / package contract (apps/executions/offline/compat.py)
export const EVALUATOR_VERSION = 3; // the rules evaluator (rules-engine.js); 2 = evidence (REQUIRE_EVIDENCE, photo/file/signature answers); 3 = RAISE_FINDING understood (server-side effect only)
export const DB_NAME = "checklist-offline";
export const DB_VERSION = 2;

export const STORES = {
  meta: "meta",
  packages: "packages",
  responses: "responses",
  operations: "operations",
  conflicts: "conflicts",
  syncState: "syncState",
  evidence: "evidence", // Phase 11: queued evidence files (Blobs) and their upload state
};

/** Package states: only ``ready`` may be opened offline; ``preparing`` is an interrupted or in-flight download. */
export const PACKAGE_STATE = { preparing: "preparing", ready: "ready", failed: "failed" };
/** Local answer states shown to the user (never "synced" until the server acknowledged it). */
export const SYNC_STATE = { pending: "pending", syncing: "syncing", synced: "synced", failed: "failed", conflict: "conflict" };
/** Evidence entry states.  Only ``available`` means "verified by the server"; ``received`` means the server has the bytes but the check is pending. */
export const EVIDENCE_STATE = { local: "local", uploading: "uploading", received: "received", available: "available", failed: "failed", rejected: "rejected", quarantined: "quarantined" };
/** Queue operation states. */
export const OP_STATE = { pending: "pending", sending: "sending", failed: "failed", conflict: "conflict", rejected: "rejected", blocked: "blocked", applied: "applied" };

export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value) {
  return typeof value === "string" && UUID_RE.test(value);
}

export function newId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** The account namespace of a record: user and organization, so two people sharing a browser profile never see each other's data. */
export function namespaceOf(account) {
  if (!account || !isUuid(account.userId) || !isUuid(account.organizationId)) {
    throw new Error("A signed-in account is required.");
  }
  return `${account.userId}:${account.organizationId}`;
}

export class ValidationFailure extends Error {
  constructor(message) {
    super(message);
    this.name = "ValidationFailure";
  }
}

function need(condition, message) {
  if (!condition) {
    throw new ValidationFailure(message);
  }
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Validate a package received from the server before it is stored.  Throws ``ValidationFailure`` with a reason. */
export function validatePackage(pkg) {
  need(isObject(pkg), "The package is not an object.");
  need(pkg.schema_version === SCHEMA_VERSION, "The package uses a newer or older protocol; update the application.");
  need(pkg.evaluator_version === EVALUATOR_VERSION, "The package needs a different rules evaluator; update the application.");
  need(isObject(pkg.assignment) && isUuid(pkg.assignment.id), "The package has no valid assignment.");
  need(isObject(pkg.checklist) && isUuid(pkg.checklist.version_id), "The package has no valid checklist version.");
  need(Array.isArray(pkg.sections) && pkg.sections.length > 0, "The package has no sections.");
  need(Array.isArray(pkg.items) && pkg.items.length > 0, "The package has no questions.");
  need(Array.isArray(pkg.rules), "The package has no rule list.");
  need(typeof pkg.package_hash === "string" && /^[0-9a-f]{64}$/.test(pkg.package_hash), "The package has no integrity hash.");
  const sectionCodes = new Set();
  for (const s of pkg.sections) {
    need(isObject(s) && isUuid(s.id) && typeof s.code === "string" && typeof s.name === "string", "A section is malformed.");
    need(!sectionCodes.has(s.code), "Duplicate section code.");
    sectionCodes.add(s.code);
  }
  const itemCodes = new Set();
  const itemIds = new Set();
  for (const i of pkg.items) {
    need(isObject(i) && isUuid(i.id) && typeof i.code === "string" && typeof i.question === "string", "A question is malformed.");
    need(sectionCodes.has(i.section), "A question belongs to an unknown section.");
    need(!itemCodes.has(i.code) && !itemIds.has(i.id), "Duplicate question.");
    need(isObject(i.config) && Array.isArray(i.options), "A question has no configuration.");
    itemCodes.add(i.code);
    itemIds.add(i.id);
  }
  for (const r of pkg.rules) {
    need(isObject(r) && typeof r.code === "string" && Array.isArray(r.actions), "A rule is malformed.");
  }
  if (pkg.execution) {
    need(isObject(pkg.execution) && isUuid(pkg.execution.id) && Array.isArray(pkg.execution.responses), "The saved execution is malformed.");
    for (const r of pkg.execution.responses) {
      need(itemIds.has(r.item), "A saved answer refers to an unknown question.");
    }
  }
  return pkg;
}

/** Validate a stored response record (defence against a corrupted or tampered database). */
export function validateResponseRecord(rec) {
  need(isObject(rec), "Response record is not an object.");
  need(typeof rec.key === "string" && typeof rec.ns === "string" && isUuid(rec.assignmentId) && isUuid(rec.itemId), "Response record has bad identifiers.");
  need(["answered", "not_applicable", "cleared"].includes(rec.status), "Response record has an unknown status.");
  need(Object.values(SYNC_STATE).includes(rec.syncState), "Response record has an unknown sync state.");
  need(Number.isInteger(rec.baseRevision) && rec.baseRevision >= 0, "Response record has a bad base revision.");
  need(Number.isInteger(rec.localRevision) && rec.localRevision >= 0, "Response record has a bad local revision.");
  return rec;
}

export function validateOperationRecord(rec) {
  need(isObject(rec), "Operation record is not an object.");
  need(isUuid(rec.opId) && typeof rec.ns === "string" && isUuid(rec.assignmentId), "Operation record has bad identifiers.");
  need(["upsert_response", "clear_response", "submit"].includes(rec.type), "Operation record has an unknown type.");
  need(Object.values(OP_STATE).includes(rec.state), "Operation record has an unknown state.");
  need(Number.isInteger(rec.seq) && rec.seq >= 0, "Operation record has a bad sequence number.");
  return rec;
}

/** Validate a stored evidence entry (the database is untrusted: a tampered record must not reach the upload code). */
export function validateEvidenceRecord(rec) {
  need(isObject(rec), "Evidence record is not an object.");
  need(isUuid(rec.id) && typeof rec.ns === "string" && isUuid(rec.assignmentId) && isUuid(rec.itemId), "Evidence record has bad identifiers.");
  need(Object.values(EVIDENCE_STATE).includes(rec.state), "Evidence record has an unknown state.");
  need(typeof rec.name === "string" && rec.name.length > 0 && rec.name.length <= 255 && !/[\\/]/.test(rec.name), "Evidence record has a bad file name.");
  need(Number.isInteger(rec.size) && rec.size > 0, "Evidence record has a bad size.");
  return rec;
}