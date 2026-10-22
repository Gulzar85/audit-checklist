/* Conflict handling.  A conflict means the server did NOT apply a local change (the answer changed elsewhere, or the execution was
 * submitted / cancelled).  Both versions are kept - the local one in the ``conflicts`` store, the server's in the result the server
 * sent - and the user decides.  Nothing is merged and nothing is overwritten automatically.
 *
 * Resolutions (revalidated and audited by the server, POST /api/v1/offline/resolve-conflict/):
 *   keep_server  - take the server's value; the local value is dropped.
 *   use_local    - send the local value again, based on the server's current revision (the server may refuse it or find a new conflict).
 *   keep for later / export - leave it unresolved; ``recoveryExport`` produces a file of the local answers.
 * Conflicts about a submitted or cancelled execution cannot be resolved here (the server will not reopen it); the local answer is
 * preserved until the user exports and explicitly discards it.
 */
import { SCHEMA_VERSION, STORES, SYNC_STATE, newId } from "./schema.js";
import { TERMINAL_BLOCKERS, responseKey } from "./sync-queue.js";

export class ConflictHandler {
  constructor({ ctx, getSession, fetchImpl = (...args) => globalThis.fetch(...args), connectivity }) {
    Object.assign(this, { ctx, getSession, fetchImpl, connectivity });
  }

  list() {
    return this.ctx.storage.getAllByIndex(STORES.conflicts, "byNs", this.ctx.ns);
  }

  resolvable(conflict) {
    return conflict.code === "revision_mismatch" && conflict.type !== "submit";
  }

  async _post(body) {
    const session = await this.getSession();
    if (session.status !== "ok") {
      return { status: "unavailable", message: session.status === "offline" ? "You are offline." : "Sign in again to resolve this conflict." };
    }
    let response;
    try {
      response = await this.fetchImpl("/api/v1/offline/resolve-conflict/", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": session.csrfToken || "" }, body: JSON.stringify(body),
      });
    } catch (error) {
      this.connectivity && this.connectivity.report(false);
      return { status: "unavailable", message: "You are offline." };
    }
    if (!response.ok) {
      return { status: "unavailable", message: "The server could not process the resolution." };
    }
    const data = await response.json();
    return data && data.data ? data.data : data;
  }

  async _resolutionId(conflict) {
    if (conflict.resolutionOpId) {
      return conflict.resolutionOpId; // reuse on retry: the server answers a repeat with the original outcome
    }
    const id = newId();
    await this.ctx.storage.put(STORES.conflicts, { ...conflict, resolutionOpId: id });
    return id;
  }

  async _finish(conflict, serverValue, revision) {
    const { ctx } = this;
    const key = responseKey(ctx.ns, conflict.assignmentId, conflict.itemId);
    const batch = [{ type: "delete", store: STORES.conflicts, key: conflict.opId }, { type: "delete", store: STORES.operations, key: conflict.opId }];
    if (serverValue && serverValue.exists) {
      batch.push({ type: "put", store: STORES.responses, record: {
        key, ns: ctx.ns, assignmentId: conflict.assignmentId, itemId: conflict.itemId, status: serverValue.status, value: serverValue.value, comment: serverValue.comment || "",
        baseRevision: revision, localRevision: 0, syncState: SYNC_STATE.synced, error: null, opId: null, updatedAt: ctx.now(),
      } });
    } else {
      batch.push({ type: "delete", store: STORES.responses, key });
    }
    await ctx.storage.batch(batch);
  }

  async keepServer(conflict) {
    const resolutionId = await this._resolutionId(conflict);
    const result = await this._post({ operation_id: resolutionId, conflict_operation_id: conflict.opId, resolution: "keep_server", version_id: await this._versionId(conflict), item_id: conflict.itemId });
    if (result.status !== "applied") {
      return result;
    }
    const server = result.server || conflict.server;
    await this._finish(conflict, server, server ? server.revision : 0);
    return result;
  }

  async useLocal(conflict) {
    if (!conflict.local || !conflict.server) {
      return { status: "rejected", message: "There is nothing to re-send." };
    }
    const resolutionId = await this._resolutionId(conflict);
    const result = await this._post({
      operation_id: resolutionId, conflict_operation_id: conflict.opId, resolution: "use_local", version_id: await this._versionId(conflict), item_id: conflict.itemId,
      value: conflict.local.value, not_applicable: !!conflict.local.not_applicable, comment: conflict.local.comment, base_revision: conflict.server.revision,
    });
    if (result.status === "applied") {
      const revision = Number.isInteger(result.response_revision) ? result.response_revision : 0;
      const local = conflict.local;
      const cleared = !local.not_applicable && (local.value === null || local.value === undefined || local.value === "");
      await this._finish(conflict, { exists: !cleared, status: local.not_applicable ? "not_applicable" : "answered", value: local.value, comment: local.comment }, revision);
    } else if (result.status === "conflict" && result.server) {
      await this.ctx.storage.put(STORES.conflicts, { ...conflict, server: result.server, resolutionOpId: null, message: result.message || conflict.message });
    }
    return result;
  }

  async _versionId(conflict) {
    const pkg = await this.ctx.storage.get(STORES.packages, `${this.ctx.ns}|${conflict.assignmentId}`);
    return pkg ? pkg.versionId : null;
  }

  /** The user chose to give up the local value of a conflict that cannot be resolved.  Call only after the explicit warning. */
  async discard(conflict) {
    const key = responseKey(this.ctx.ns, conflict.assignmentId, conflict.itemId);
    await this.ctx.storage.batch([
      { type: "delete", store: STORES.conflicts, key: conflict.opId }, { type: "delete", store: STORES.operations, key: conflict.opId },
      { type: "delete", store: STORES.responses, key },
    ]);
  }

  isTerminal(conflict) {
    return TERMINAL_BLOCKERS.includes(conflict.code);
  }

  /** A plain JSON document of the local answers and conflicts of one assignment, for manual recovery. */
  async recoveryExport(assignmentId) {
    const { ctx } = this;
    const pkg = await ctx.storage.get(STORES.packages, `${ctx.ns}|${assignmentId}`);
    const byId = pkg ? Object.fromEntries(pkg.package.items.map((i) => [i.id, i])) : {};
    const responses = (await ctx.storage.getAllByIndex(STORES.responses, "byAssignment", assignmentId)).map((r) => ({
      question: (byId[r.itemId] || {}).question || r.itemId, code: (byId[r.itemId] || {}).code, status: r.status, value: r.value, comment: r.comment, syncState: r.syncState,
    }));
    const conflicts = (await ctx.storage.getAllByIndex(STORES.conflicts, "byAssignment", assignmentId)).map((c) => ({
      question: (byId[c.itemId] || {}).question || c.itemId, reason: c.code, local: c.local, server: c.server,
    }));
    const evidence = (await ctx.storage.getAllByIndex(STORES.evidence, "byAssignment", assignmentId)).filter((e) => e.ns === ctx.ns).map((e) => ({
      question: (byId[e.itemId] || {}).question || e.itemId, name: e.name, size: e.size, state: e.state, error: e.error, note: "File contents are not included in this export; use Save a copy in the app.",
    }));
    return { schema_version: SCHEMA_VERSION, assignment_id: assignmentId, exported_at: new Date(ctx.now()).toISOString(), responses, conflicts, evidence };
  }
}

