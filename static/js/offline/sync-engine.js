/* The synchronization engine: one loop at a time, driven by explicit triggers (Sync now, coming back online, Background Sync,
 * a bounded retry timer) - never by continuous polling.
 *
 * It re-asserts identity with the server first (a revoked or changed account stops the run), sends ready queue entries in sequence
 * order, and only then - after the server's answer was received and validated - updates local records (sync-queue.applyResult).
 * An entry is never discarded because a request was sent; a lost response is recovered by retrying the same operation id.
 */
import { SCHEMA_VERSION, STORES } from "./schema.js";
import { applyResult, markSending, markTransientFailure, queueSummary, recoverInterrupted, resetFailed, takeBatch } from "./sync-queue.js";
import { EVIDENCE_STATE } from "./schema.js";
import { applyStatus, applyUploadResponse, formDataFor, markTransient, pendingUploads, receivedEntries, recoverUploading, resetFailedEvidence } from "./evidence-store.js";

const STATUSES = new Set(["applied", "conflict", "rejected", "blocked"]);

export function toWire(op) {
  const wire = { operation_id: op.opId, type: op.type, seq: op.seq, assignment_id: op.assignmentId, version_id: op.versionId, schema_version: SCHEMA_VERSION };
  if (op.type !== "submit") {
    Object.assign(wire, { item_id: op.itemId, value: op.payload.value, not_applicable: !!op.payload.not_applicable, comment: op.payload.comment, base_revision: op.baseRevision });
  }
  if (op.createdAt) {
    wire.client_created_at = new Date(op.createdAt).toISOString(); // diagnostic only: the server never trusts it
  }
  return wire;
}

export class SyncEngine {
  constructor({ ctx, connectivity, getSession, fetchImpl = (...args) => globalThis.fetch(...args), onChange = () => {}, limit = 100, setTimer = setTimeout, clearTimer = clearTimeout, locks = globalThis.navigator && globalThis.navigator.locks }) {
    Object.assign(this, { ctx, connectivity, getSession, fetchImpl, onChange, limit, setTimer, clearTimer, locks });
    this.status = "idle"; // idle | syncing | offline | auth | account_changed | error
    this.lastError = null;
    this.running = null;
    this.timer = null;
  }

  setStatus(status, error = null) {
    this.status = status;
    this.lastError = error;
    this.onChange(this.snapshot());
  }

  snapshot() {
    return { status: this.status, error: this.lastError };
  }

  async start() {
    await recoverInterrupted(this.ctx);
    await recoverUploading(this.ctx);
    return this.run();
  }

  /** Sync now.  Concurrent calls (same tab, or other tabs when Web Locks exist) share one run - two loops never touch one queue. */
  run({ force = false } = {}) {
    if (this.running) {
      return this.running;
    }
    const job = async () => {
      if (force) {
        await resetFailed(this.ctx);
        await resetFailedEvidence(this.ctx);
      }
      return this._run();
    };
    const guarded = this.locks && this.locks.request
      ? this.locks.request(`checklist-sync:${this.ctx.ns}`, { ifAvailable: true }, (lock) => (lock ? job() : { skipped: true }))
      : job();
    this.running = Promise.resolve(guarded).finally(() => {
      this.running = null;
    });
    return this.running;
  }

  async _run() {
    this.clearRetry();
    this.setStatus("syncing");
    const session = await this.getSession();
    if (session.status === "offline") {
      this.connectivity.report(false);
      this.setStatus("offline");
      await this.scheduleRetry();
      return { ok: false, reason: "offline" };
    }
    this.connectivity.report(true);
    if (session.status !== "ok") {
      this.setStatus("auth", "Sign in again to synchronize. Your answers stay on this device.");
      return { ok: false, reason: "auth" };
    }
    const ns = `${session.account.userId}:${session.account.organizationId}`;
    if (ns !== this.ctx.ns) {
      this.setStatus("account_changed", "A different account is signed in. This device's offline work belongs to another account.");
      return { ok: false, reason: "account_changed" };
    }
    const evidence = await this._syncEvidence(session.csrfToken);
    if (evidence.stop) {
      await this.scheduleRetry();
      return { ok: false, reason: evidence.reason };
    }
    const summary = { applied: 0, conflict: 0, rejected: 0, blocked: 0, sent: 0, uploaded: evidence.uploaded };
    for (let round = 0; round < 50; round += 1) {
      const now = this.ctx.now();
      const batch = await takeBatch(this.ctx, now, this.limit);
      if (!batch.length) {
        break;
      }
      await markSending(this.ctx, batch);
      const outcome = await this._send(batch, session.csrfToken);
      if (outcome.stop) {
        await this.scheduleRetry();
        return { ok: false, reason: outcome.reason, ...summary };
      }
      summary.sent += batch.length;
      for (const label of outcome.labels) {
        summary[label] += 1;
      }
      if (outcome.labels.every((l) => l === "blocked")) {
        break; // nothing moved: do not spin
      }
    }
    const state = (await this.ctx.storage.get(STORES.syncState, this.ctx.ns)) || { ns: this.ctx.ns, seq: 0 };
    await this.ctx.storage.put(STORES.syncState, { ...state, lastSuccessAt: this.ctx.now(), lastError: null });
    this.setStatus("idle");
    await this.scheduleRetry();
    return { ok: true, ...summary };
  }

  /** Upload queued evidence (before the answer operations, so a submit always follows its files), then ask about files still being checked. */
  async _syncEvidence(csrfToken) {
    const { ctx } = this;
    let uploaded = 0;
    for (const entry of await pendingUploads(ctx, ctx.now())) {
      await ctx.storage.put(STORES.evidence, { ...entry, state: EVIDENCE_STATE.uploading });
      const pkg = await ctx.storage.get(STORES.packages, `${ctx.ns}|${entry.assignmentId}`);
      let response;
      try {
        response = await this.fetchImpl("/api/v1/evidence/uploads/", {
          method: "POST", credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json", "X-CSRFToken": csrfToken || "" },
          body: formDataFor(entry, pkg && pkg.executionId), // no Content-Type header: the browser sets the multipart boundary
        });
      } catch (error) {
        this.connectivity.report(false);
        await markTransient(ctx, entry, "No connection to the server.", 0);
        this.setStatus("offline");
        return { stop: true, reason: "offline", uploaded };
      }
      this.connectivity.report(true);
      const outcome = await applyUploadResponse(ctx, entry, response);
      if (outcome === "auth") {
        await ctx.storage.put(STORES.evidence, { ...entry, state: EVIDENCE_STATE.local });
        this.setStatus("auth", "Sign in again to synchronize. Your files stay on this device.");
        return { stop: true, reason: "auth", uploaded };
      }
      if (outcome === "ok") {
        uploaded += 1;
      }
    }
    for (const entry of await receivedEntries(ctx)) {
      try {
        const response = await this.fetchImpl(`/api/v1/evidence/${entry.serverAssetId}/status/`, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
        await applyStatus(ctx, entry, response);
      } catch (error) {
        this.connectivity.report(false);
        break;
      }
    }
    this.onChange(this.snapshot());
    return { stop: false, uploaded };
  }

  async _send(batch, csrfToken) {
    let response;
    try {
      response = await this.fetchImpl("/api/v1/offline/sync/", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrfToken || "" },
        body: JSON.stringify({ schema_version: SCHEMA_VERSION, operations: batch.map(toWire) }),
      });
    } catch (error) {
      this.connectivity.report(false);
      await markTransientFailure(this.ctx, batch, "No connection to the server.", this.ctx.now());
      this.setStatus("offline");
      return { stop: true, reason: "offline", labels: [] };
    }
    this.connectivity.report(true);
    if (response.status === 401 || response.status === 403) {
      await markTransientFailure(this.ctx, batch.map((o) => ({ ...o, attempts: -1 })), "Sign in again.", this.ctx.now()); // not counted against the retry budget
      this.setStatus("auth", "Sign in again to synchronize. Your answers stay on this device.");
      return { stop: true, reason: "auth", labels: [] };
    }
    if (response.status === 429 || response.status >= 500) {
      const wait = Number(response.headers && response.headers.get && response.headers.get("Retry-After")) * 1000 || 0;
      await markTransientFailure(this.ctx, batch, `The server could not process the request (${response.status}).`, this.ctx.now(), wait);
      this.setStatus("error", "The server is busy or unavailable. Will retry.");
      return { stop: true, reason: "server", labels: [] };
    }
    let body = null;
    try {
      body = await response.json();
    } catch (error) {
      body = null;
    }
    const data = body && body.data ? body.data : body;
    if (response.status >= 400 || !data || !Array.isArray(data.results)) {
      const message = (body && body.error && body.error.message) || "The server refused the synchronization request.";
      await this.ctx.storage.batch(batch.map((o) => ({ type: "put", store: STORES.operations, record: { ...o, state: "failed", lastError: message } })));
      this.setStatus("error", message);
      return { stop: true, reason: "refused", labels: [] };
    }
    const byId = new Map(data.results.filter((r) => r && STATUSES.has(r.status)).map((r) => [r.operation_id, r]));
    const labels = [];
    for (const op of batch) {
      const result = byId.get(op.opId);
      if (!result) {
        await markTransientFailure(this.ctx, [op], "The server gave no answer for this change.", this.ctx.now());
        continue;
      }
      labels.push(await applyResult(this.ctx, op, result));
    }
    this.onChange(this.snapshot());
    return { stop: false, labels };
  }

  clearRetry() {
    if (this.timer) {
      this.clearTimer(this.timer);
      this.timer = null;
    }
  }

  /** One timer for the earliest retry - not a polling loop. */
  async scheduleRetry() {
    this.clearRetry();
    const ops = await this.ctx.storage.getAllByIndex(STORES.operations, "byNs", this.ctx.ns);
    const waiting = ops.filter((o) => o.state === "pending" && o.nextAttemptAt > this.ctx.now()).map((o) => o.nextAttemptAt);
    if (waiting.length) {
      const delay = Math.max(1000, Math.min(...waiting) - this.ctx.now());
      this.timer = this.setTimer(() => this.run(), delay);
    }
  }

  summary() {
    return queueSummary(this.ctx);
  }
}
