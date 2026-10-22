/* The persistent synchronization queue: planning local writes (with safe coalescing), choosing batches, and applying server results.
 *
 * Rules (documented in docs/offline-sync-protocol.md):
 *  - one queue entry per logical change, with a stable ``opId`` created once and reused on every retry (server idempotency key);
 *  - repeated edits of one answer that has not been sent are COALESCED into the same entry (latest value wins, the id is kept);
 *  - an edit made while that answer's entry is IN FLIGHT becomes a second entry that depends on the first (its base revision is
 *    filled in from the server's acknowledgement) - an older request can never overwrite a newer local value;
 *  - an entry is only removed after the server acknowledged it; nothing is deleted merely because it was sent;
 *  - submit is only offered to the server once nothing else for that assignment is unresolved.
 */
import { OP_STATE, STORES, SYNC_STATE, newId, validateOperationRecord } from "./schema.js";

export const MAX_ATTEMPTS = 6;
export const BASE_BACKOFF_MS = 2000;
export const MAX_BACKOFF_MS = 5 * 60 * 1000;
export const TERMINAL_BLOCKERS = ["assignment_cancelled", "execution_cancelled", "execution_submitted", "unavailable", "forbidden", "stale_version", "execution_mismatch"];

export const responseKey = (ns, assignmentId, itemId) => `${ns}|${assignmentId}|${itemId}`;
export const packageKey = (ns, assignmentId) => `${ns}|${assignmentId}`;

export function backoffMs(attempts) {
  return Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** Math.max(0, attempts - 1));
}

export class LocalConflict extends Error {
  constructor(message) {
    super(message);
    this.name = "LocalConflict";
  }
}

async function nextSeq(ctx, extra = []) {
  const state = (await ctx.storage.get(STORES.syncState, ctx.ns)) || { ns: ctx.ns, seq: 0 };
  const seq = state.seq + 1;
  extra.push({ type: "put", store: STORES.syncState, record: { ...state, seq } });
  return seq;
}

/** Queue (or coalesce) the write of one answer.  Returns the ops to apply atomically together with the response record. */
export async function planResponseWrite(ctx, { assignmentId, versionId, itemId, status, value, comment, baseRevision }) {
  const key = responseKey(ctx.ns, assignmentId, itemId);
  const existing = await ctx.storage.get(STORES.responses, key);
  const all = (await ctx.storage.getAllByIndex(STORES.operations, "byAssignment", assignmentId)).filter((o) => o.itemId === itemId && o.type !== "submit");
  if (all.some((o) => o.state === OP_STATE.conflict)) {
    throw new LocalConflict("This answer has a synchronization conflict. Resolve it before editing.");
  }
  const batch = [];
  for (const rejected of all.filter((o) => o.state === OP_STATE.rejected)) {
    batch.push({ type: "delete", store: STORES.operations, key: rejected.opId }); // superseded by this new edit
  }
  const sending = all.find((o) => o.state === OP_STATE.sending);
  const open = all.find((o) => [OP_STATE.pending, OP_STATE.failed, OP_STATE.blocked].includes(o.state));
  const known = existing ? existing.baseRevision : baseRevision;
  const localRevision = (existing ? existing.localRevision : 0) + 1;
  const cleared = status === "cleared";

  // Clearing an answer the server has never seen: nothing to send - drop the local record and its entry.
  if (cleared && !sending && (!existing || (existing.baseRevision === 0 && (!open || (open.baseRevision === 0 && !open.dependsOn))))) {
    if (open) {
      batch.push({ type: "delete", store: STORES.operations, key: open.opId });
    }
    if (existing) {
      batch.push({ type: "delete", store: STORES.responses, key });
    }
    return { batch, op: null, record: null };
  }

  const type = cleared ? "clear_response" : "upsert_response";
  const payload = { value: cleared ? null : value, not_applicable: status === "not_applicable", comment: cleared ? "" : comment || "" };
  let op;
  if (open) {
    op = { ...open, type, payload, localRevision, state: OP_STATE.pending, attempts: 0, nextAttemptAt: 0, lastError: null, seq: await nextSeq(ctx, batch) };
  } else {
    op = {
      opId: newId(), ns: ctx.ns, assignmentId, versionId, type, itemId, payload, localRevision, state: OP_STATE.pending, attempts: 0, nextAttemptAt: 0,
      lastError: null, createdAt: ctx.now(), seq: await nextSeq(ctx, batch),
      baseRevision: sending ? null : known, dependsOn: sending ? sending.opId : null,
    };
  }
  validateOperationRecord(op);
  const record = {
    key, ns: ctx.ns, assignmentId, itemId, status, value: cleared ? null : value, comment: cleared ? "" : comment || "", baseRevision: known, localRevision,
    syncState: SYNC_STATE.pending, error: null, opId: op.opId, updatedAt: ctx.now(),
  };
  batch.push({ type: "put", store: STORES.operations, record: op }, { type: "put", store: STORES.responses, record });
  return { batch, op, record };
}

export async function planSubmit(ctx, { assignmentId, versionId }) {
  const ops = await ctx.storage.getAllByIndex(STORES.operations, "byAssignment", assignmentId);
  if (ops.some((o) => o.type === "submit" && o.state !== OP_STATE.rejected)) {
    return { batch: [], op: null }; // already queued
  }
  const batch = ops.filter((o) => o.type === "submit" && o.state === OP_STATE.rejected).map((o) => ({ type: "delete", store: STORES.operations, key: o.opId }));
  const op = {
    opId: newId(), ns: ctx.ns, assignmentId, versionId, type: "submit", itemId: null, payload: {}, localRevision: 0, state: OP_STATE.pending, attempts: 0,
    nextAttemptAt: 0, lastError: null, createdAt: ctx.now(), seq: await nextSeq(ctx, batch), baseRevision: null, dependsOn: null,
  };
  batch.push({ type: "put", store: STORES.operations, record: op });
  return { batch, op };
}

/** What may be sent now: ready entries in sequence order (never an entry waiting on an in-flight one). */
export async function takeBatch(ctx, now, limit) {
  const ops = (await ctx.storage.getAllByIndex(STORES.operations, "byNs", ctx.ns)).sort((a, b) => a.seq - b.seq);
  const ready = (o) => [OP_STATE.pending, OP_STATE.blocked].includes(o.state) && !o.dependsOn && (o.nextAttemptAt || 0) <= now;
  const unverified = new Set(
    (await ctx.storage.getAllByIndex(STORES.evidence, "byNs", ctx.ns)).filter((e) => ["local", "uploading", "received", "failed"].includes(e.state)).map((e) => e.assignmentId),
  );
  const chosen = [];
  for (const op of ops.filter(ready)) {
    if (chosen.length >= limit) {
      break;
    }
    if (op.type === "submit") {
      if (unverified.has(op.assignmentId)) {
        continue; // evidence is still being uploaded or checked: the server only counts verified files, so submitting now would be refused
      }
      const others = ops.filter((o) => o.assignmentId === op.assignmentId && o.type !== "submit");
      const unresolved = others.some((o) => !chosen.includes(o) && o.state !== OP_STATE.applied);
      if (unresolved) {
        continue; // submit waits until every answer of this checklist was accepted (or is in this very batch, ahead of it)
      }
    }
    chosen.push(op);
  }
  return chosen;
}

export async function markSending(ctx, ops) {
  await ctx.storage.batch(ops.map((o) => ({ type: "put", store: STORES.operations, record: { ...o, state: OP_STATE.sending } })));
}

/** After a reload or crash: entries left "sending" return to pending.  Safe - their ids make the retry idempotent. */
export async function recoverInterrupted(ctx) {
  const stuck = (await ctx.storage.getAllByIndex(STORES.operations, "byNs", ctx.ns)).filter((o) => o.state === OP_STATE.sending);
  if (stuck.length) {
    await ctx.storage.batch(stuck.map((o) => ({ type: "put", store: STORES.operations, record: { ...o, state: OP_STATE.pending } })));
  }
  return stuck.length;
}

/** A transient failure (network, 5xx): back to pending with bounded exponential backoff; give up (``failed``) after MAX_ATTEMPTS. */
export async function markTransientFailure(ctx, ops, message, now, retryAfterMs = 0) {
  await ctx.storage.batch(ops.map((o) => {
    const attempts = (o.attempts || 0) + 1;
    const exhausted = attempts >= MAX_ATTEMPTS;
    return { type: "put", store: STORES.operations, record: {
      ...o, attempts, lastError: message, state: exhausted ? OP_STATE.failed : OP_STATE.pending, nextAttemptAt: now + Math.max(retryAfterMs, backoffMs(attempts)),
    } };
  }));
}

/** "Sync now": failed entries get a fresh start. */
export async function resetFailed(ctx) {
  const failed = (await ctx.storage.getAllByIndex(STORES.operations, "byNs", ctx.ns)).filter((o) => o.state === OP_STATE.failed || (o.state === OP_STATE.pending && o.nextAttemptAt));
  if (failed.length) {
    await ctx.storage.batch(failed.map((o) => ({ type: "put", store: STORES.operations, record: { ...o, state: OP_STATE.pending, attempts: 0, nextAttemptAt: 0 } })));
  }
}

/** Apply one server result to the local data.  Returns the outcome label for the UI.  Local data changes only here, after the
 *  server's answer arrived and was validated by the caller. */
export async function applyResult(ctx, op, result) {
  const batch = [];
  const key = op.itemId ? responseKey(ctx.ns, op.assignmentId, op.itemId) : null;
  const response = key ? await ctx.storage.get(STORES.responses, key) : null;
  const pkg = await ctx.storage.get(STORES.packages, packageKey(ctx.ns, op.assignmentId));
  const status = result.status;
  if (status === "applied") {
    batch.push({ type: "delete", store: STORES.operations, key: op.opId });
    if (op.type === "submit") {
      if (pkg) {
        batch.push({ type: "put", store: STORES.packages, record: { ...pkg, submitState: "submitted", executionStatus: "submitted", executionId: result.execution || pkg.executionId, blockedReason: null } });
      }
    } else {
      if (pkg && result.execution) {
        batch.push({ type: "put", store: STORES.packages, record: { ...pkg, executionId: result.execution } });
      }
      const revision = Number.isInteger(result.response_revision) ? result.response_revision : 0;
      if (response && response.localRevision === op.localRevision) {
        if (op.type === "clear_response") {
          batch.push({ type: "delete", store: STORES.responses, key });
        } else {
          batch.push({ type: "put", store: STORES.responses, record: { ...response, syncState: SYNC_STATE.synced, baseRevision: revision, error: null, opId: null } });
        }
      } else if (response) {
        batch.push({ type: "put", store: STORES.responses, record: { ...response, baseRevision: revision } });
      }
      const dependents = (await ctx.storage.getAllByIndex(STORES.operations, "byAssignment", op.assignmentId)).filter((o) => o.dependsOn === op.opId);
      for (const d of dependents) {
        batch.push({ type: "put", store: STORES.operations, record: { ...d, dependsOn: null, baseRevision: revision } });
      }
    }
    await ctx.storage.batch(batch);
    return "applied";
  }
  if (status === "conflict") {
    batch.push({ type: "put", store: STORES.operations, record: { ...op, state: OP_STATE.conflict, lastError: result.message || result.code } });
    batch.push({ type: "put", store: STORES.conflicts, record: {
      opId: op.opId, ns: ctx.ns, assignmentId: op.assignmentId, itemId: op.itemId, type: op.type, code: result.code, message: result.message || "",
      local: op.type === "submit" ? null : { ...op.payload }, server: result.server || null, createdAt: ctx.now(), resolutionOpId: null,
    } });
    if (response) {
      batch.push({ type: "put", store: STORES.responses, record: { ...response, syncState: SYNC_STATE.conflict, error: result.message || null } });
    }
    if (pkg && TERMINAL_BLOCKERS.includes(result.code)) {
      batch.push({ type: "put", store: STORES.packages, record: { ...pkg, blockedReason: result.code, ...(result.code === "execution_submitted" ? { executionStatus: "submitted" } : {}) } });
    }
    await ctx.storage.batch(batch);
    return "conflict";
  }
  if (status === "blocked") {
    batch.push({ type: "put", store: STORES.operations, record: { ...op, state: OP_STATE.pending, lastError: result.message || null, nextAttemptAt: 0 } });
    await ctx.storage.batch(batch);
    return "blocked";
  }
  // rejected (validation failed, unavailable, forbidden, stale version, malformed ...): a final answer for this payload.
  const messages = (result.errors || []).map((e) => e.message).concat(result.message ? [result.message] : []);
  batch.push({ type: "put", store: STORES.operations, record: { ...op, state: OP_STATE.rejected, lastError: messages.join(" ") || result.code, errors: result.errors || result.problems || null, code: result.code } });
  if (response) {
    batch.push({ type: "put", store: STORES.responses, record: { ...response, syncState: SYNC_STATE.failed, error: messages.join(" ") || result.code } });
  }
  if (pkg && TERMINAL_BLOCKERS.includes(result.code)) {
    batch.push({ type: "put", store: STORES.packages, record: { ...pkg, blockedReason: result.code } });
  }
  if (pkg && op.type === "submit" && result.code === "validation_failed") {
    batch.push({ type: "put", store: STORES.packages, record: { ...pkg, submitProblems: result.problems || null } });
  }
  await ctx.storage.batch(batch);
  return "rejected";
}

export async function queueSummary(ctx) {
  const ops = await ctx.storage.getAllByIndex(STORES.operations, "byNs", ctx.ns);
  const count = (...states) => ops.filter((o) => states.includes(o.state)).length;
  return {
    pending: count(OP_STATE.pending, OP_STATE.blocked), sending: count(OP_STATE.sending), failed: count(OP_STATE.failed), conflicts: count(OP_STATE.conflict),
    rejected: count(OP_STATE.rejected), total: ops.length,
  };
}
