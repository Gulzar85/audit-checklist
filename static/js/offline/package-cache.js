/* Preparing assignments for offline use, and removing them again.
 *
 * Preparation downloads the server's *package* (the exact pinned definition + metadata), validates it, and only then promotes it to
 * ``ready`` - in ONE atomic write that also seeds the saved server answers, followed by a read-back verification.  Until that
 * write completes the assignment is not ready: an interrupted download leaves at most a ``preparing`` / ``failed`` staging record,
 * which the UI shows as "not ready".  A ready package is immutable for its execution: preparing again never replaces a package that
 * still has unsynchronized local work, and a newer checklist version is never swapped in silently.
 */
import { PACKAGE_STATE, STORES, SYNC_STATE, ValidationFailure, validatePackage } from "./schema.js";
import { packageKey, responseKey } from "./sync-queue.js";

const stagingKey = (ns, assignmentId) => `${ns}|${assignmentId}|staging`;

export async function unsyncedCount(ctx, assignmentId) {
  const responses = await ctx.storage.getAllByIndex(STORES.responses, "byAssignment", assignmentId);
  const ops = await ctx.storage.getAllByIndex(STORES.operations, "byAssignment", assignmentId);
  const conflicts = await ctx.storage.getAllByIndex(STORES.conflicts, "byAssignment", assignmentId);
  return responses.filter((r) => r.syncState !== SYNC_STATE.synced).length + ops.filter((o) => o.state !== "applied").length + conflicts.length;
}

export class PackageCache {
  constructor({ ctx, fetchImpl = (...args) => globalThis.fetch(...args) }) {
    Object.assign(this, { ctx, fetchImpl });
  }

  async get(assignmentId) {
    const rec = await this.ctx.storage.get(STORES.packages, packageKey(this.ctx.ns, assignmentId));
    return rec && rec.state === PACKAGE_STATE.ready ? rec : null;
  }

  /** Every package record of this account (ready ones and unfinished attempts) with its unsynchronized count. */
  async list() {
    const rows = await this.ctx.storage.getAllByIndex(STORES.packages, "byNs", this.ctx.ns);
    const out = [];
    for (const rec of rows) {
      out.push({ ...rec, unsynced: rec.state === PACKAGE_STATE.ready ? await unsyncedCount(this.ctx, rec.assignmentId) : 0 });
    }
    return out.sort((a, b) => (b.preparedAt || b.startedAt || 0) - (a.preparedAt || a.startedAt || 0));
  }

  async fail(assignmentId, message, reasons) {
    await this.ctx.storage.put(STORES.packages, {
      key: stagingKey(this.ctx.ns, assignmentId), ns: this.ctx.ns, assignmentId, state: PACKAGE_STATE.failed, error: message, reasons: reasons || [], startedAt: this.ctx.now(),
    }).catch(() => {});
  }

  /** Download, validate, store, verify.  Resolves ``{status: "ready" | "kept" | "failed", ...}``; never throws for expected failures. */
  async prepare(assignmentId, { onProgress = () => {} } = {}) {
    const { ctx } = this;
    const existing = await this.get(assignmentId);
    const unsynced = existing ? await unsyncedCount(ctx, assignmentId) : 0;
    if (existing && unsynced) {
      return { status: "kept", message: "This checklist still has changes that are not synchronized, so it was not replaced. Synchronize first.", unsynced };
    }
    onProgress({ step: "start", label: "Checking access…" });
    await ctx.storage.put(STORES.packages, {
      key: stagingKey(ctx.ns, assignmentId), ns: ctx.ns, assignmentId, state: PACKAGE_STATE.preparing, startedAt: ctx.now(),
    }).catch(() => {}); // only a marker: if even this fails, the real write below will report the storage problem
    let response;
    try {
      response = await this.fetchImpl(`/api/v1/offline/assignments/${assignmentId}/package/`, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
    } catch (error) {
      await this.fail(assignmentId, "You are offline. Connect to the internet to prepare this checklist.");
      return { status: "failed", message: "You are offline. Connect to the internet to prepare this checklist." };
    }
    onProgress({ step: "download", label: "Downloading the checklist…" });
    let body = null;
    try {
      body = await response.json();
    } catch (error) {
      body = null;
    }
    if (!response.ok) {
      const details = (body && body.error && body.error.details) || {};
      const message = response.status === 409 ? "This checklist cannot be used offline." : response.status === 404 ? "This assignment is not available to you." : response.status === 401 || response.status === 403 ? "Sign in again to prepare this checklist." : "The server could not provide this checklist.";
      await this.fail(assignmentId, message, details.reasons);
      return { status: "failed", message, reasons: details.reasons || [] };
    }
    const pkg = body && body.data ? body.data : body;
    try {
      onProgress({ step: "validate", label: "Verifying the download…" });
      validatePackage(pkg);
      if (pkg.assignment.id !== assignmentId) {
        throw new ValidationFailure("The package belongs to a different assignment.");
      }
      if (existing && existing.versionId !== pkg.checklist.version_id) {
        throw new ValidationFailure("The assignment now uses a different checklist version. Remove the old copy first.");
      }
    } catch (error) {
      if (error instanceof ValidationFailure) {
        await this.fail(assignmentId, error.message);
        return { status: "failed", message: error.message };
      }
      throw error;
    }
    onProgress({ step: "store", label: "Saving to this device…" });
    const key = packageKey(ctx.ns, assignmentId);
    const batch = [
      { type: "put", store: STORES.packages, record: {
        key, ns: ctx.ns, assignmentId, versionId: pkg.checklist.version_id, state: PACKAGE_STATE.ready, package: pkg, packageHash: pkg.package_hash,
        preparedAt: ctx.now(), executionId: pkg.execution ? pkg.execution.id : null, executionStatus: pkg.execution ? pkg.execution.status : null,
        submitState: null, blockedReason: null, schemaVersion: pkg.schema_version,
      } },
      { type: "delete", store: STORES.packages, key: stagingKey(ctx.ns, assignmentId) },
    ];
    if (pkg.execution) {
      for (const r of pkg.execution.responses) {
        batch.push({ type: "put", store: STORES.responses, record: {
          key: responseKey(ctx.ns, assignmentId, r.item), ns: ctx.ns, assignmentId, itemId: r.item, status: r.status, value: r.value, comment: r.comment || "",
          baseRevision: r.revision, localRevision: 0, syncState: SYNC_STATE.synced, error: null, opId: null, updatedAt: ctx.now(),
        } });
      }
    }
    try {
      await ctx.storage.batch(batch);
      const check = await ctx.storage.get(STORES.packages, key);
      if (!check || check.state !== PACKAGE_STATE.ready || check.packageHash !== pkg.package_hash) {
        throw new Error("verification failed");
      }
    } catch (error) {
      const message = error && error.kind === "quota" ? error.message : "The checklist could not be saved to this device.";
      await this.fail(assignmentId, message);
      return { status: "failed", message };
    }
    onProgress({ step: "done", label: "Ready for offline use." });
    return { status: "ready", package: pkg };
  }

  /** Remove the cached copy of one assignment.  Refuses while unsynchronized work exists unless the caller explicitly discards it. */
  async remove(assignmentId, { discardUnsynced = false } = {}) {
    const { ctx } = this;
    const unsynced = await unsyncedCount(ctx, assignmentId);
    if (unsynced && !discardUnsynced) {
      return { removed: false, unsynced };
    }
    const batch = [
      { type: "delete", store: STORES.packages, key: packageKey(ctx.ns, assignmentId) },
      { type: "delete", store: STORES.packages, key: stagingKey(ctx.ns, assignmentId) },
    ];
    for (const r of await ctx.storage.getAllByIndex(STORES.responses, "byAssignment", assignmentId)) {
      batch.push({ type: "delete", store: STORES.responses, key: r.key });
    }
    for (const o of await ctx.storage.getAllByIndex(STORES.operations, "byAssignment", assignmentId)) {
      batch.push({ type: "delete", store: STORES.operations, key: o.opId });
    }
    for (const c of await ctx.storage.getAllByIndex(STORES.conflicts, "byAssignment", assignmentId)) {
      batch.push({ type: "delete", store: STORES.conflicts, key: c.opId });
    }
    await ctx.storage.batch(batch);
    return { removed: true, unsynced: 0 };
  }

  /** Retention: drop fully synchronized copies older than ``maxAgeDays``.  Unsynchronized work is never touched. */
  async purgeExpired(maxAgeDays) {
    const cutoff = this.ctx.now() - maxAgeDays * 86400000;
    const removed = [];
    for (const rec of await this.list()) {
      if (rec.state === PACKAGE_STATE.ready && rec.unsynced === 0 && rec.preparedAt < cutoff) {
        await this.remove(rec.assignmentId);
        removed.push(rec.assignmentId);
      }
    }
    return removed;
  }
}

/** Remove the *synchronized* data of every other account on this device (never their unsynchronized work). */
export async function purgeOtherAccounts(storage, currentNs) {
  const batch = [];
  let kept = 0;
  const unsyncedAssignments = new Set();
  for (const r of await storage.getAll(STORES.responses)) {
    if (r.ns !== currentNs && r.syncState !== SYNC_STATE.synced) {
      unsyncedAssignments.add(`${r.ns}|${r.assignmentId}`);
    }
  }
  for (const o of await storage.getAll(STORES.operations)) {
    if (o.ns !== currentNs) {
      unsyncedAssignments.add(`${o.ns}|${o.assignmentId}`);
    }
  }
  for (const p of await storage.getAll(STORES.packages)) {
    if (p.ns === currentNs) {
      continue;
    }
    if (unsyncedAssignments.has(`${p.ns}|${p.assignmentId}`)) {
      kept += 1;
    } else {
      batch.push({ type: "delete", store: STORES.packages, key: p.key });
      for (const r of await storage.getAllByIndex(STORES.responses, "byAssignment", p.assignmentId)) {
        if (r.ns === p.ns) {
          batch.push({ type: "delete", store: STORES.responses, key: r.key });
        }
      }
    }
  }
  if (batch.length) {
    await storage.batch(batch);
  }
  return { removed: batch.filter((b) => b.store === STORES.packages).length, kept };
}
