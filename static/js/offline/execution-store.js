/* Local execution: open a prepared assignment, save answers to IndexedDB, queue them for synchronization, and queue submission.
 *
 * "Saved on this device" is only ever reported after the IndexedDB transaction committed.  Nothing here claims server confirmation:
 * a response is ``synced`` only after the server acknowledged its operation (sync-queue.applyResult), and the checklist is
 * ``submitted`` only after the server accepted the submit operation.  Writes are serialized (one at a time per store instance), so a
 * slower earlier write can never land after - and overwrite - a newer one.
 */
import { evaluate, problems as localProblems, progress as localProgress } from "./rules-engine.js";
import { normalize, isEmpty } from "./validators.js";
import { PACKAGE_STATE, STORES, SYNC_STATE } from "./schema.js";
import { LocalConflict, packageKey, planResponseWrite, planSubmit } from "./sync-queue.js";

export class ExecutionStore {
  constructor({ ctx }) {
    this.ctx = ctx;
    this.chain = Promise.resolve();
  }

  serialized(task) {
    const run = this.chain.then(task, task);
    this.chain = run.catch(() => {});
    return run;
  }

  /** The workspace state of a ready package: items, evaluation, progress, per-answer sync states. */
  async open(assignmentId) {
    const record = await this.ctx.storage.get(STORES.packages, packageKey(this.ctx.ns, assignmentId));
    if (!record || record.state !== PACKAGE_STATE.ready) {
      return null;
    }
    const rows = await this.ctx.storage.getAllByIndex(STORES.responses, "byAssignment", assignmentId);
    const pkg = record.package;
    const byId = Object.fromEntries(pkg.items.map((i) => [i.id, i]));
    const responses = {};
    const answers = {};
    const notApplicable = [];
    for (const r of rows) {
      const item = byId[r.itemId];
      if (!item || r.status === "cleared") {
        continue;
      }
      responses[item.code] = r;
      if (r.status === "not_applicable") {
        notApplicable.push(item.code);
      } else {
        answers[item.code] = r.value;
      }
    }
    const evaluation = evaluate(pkg, answers, notApplicable);
    const ops = await this.ctx.storage.getAllByIndex(STORES.operations, "byAssignment", assignmentId);
    const counts = { local: 0, pending: 0, syncing: 0, synced: 0, failed: 0, conflict: 0 };
    for (const r of Object.values(responses)) {
      counts[r.syncState === SYNC_STATE.pending ? "pending" : r.syncState === SYNC_STATE.syncing ? "syncing" : r.syncState] += 1;
    }
    const sendingIds = new Set(ops.filter((o) => o.state === "sending").map((o) => o.opId));
    for (const r of Object.values(responses)) {
      if (r.syncState === SYNC_STATE.pending && sendingIds.has(r.opId)) {
        counts.pending -= 1;
        counts.syncing += 1;
      }
    }
    const submitOp = ops.find((o) => o.type === "submit");
    const readOnly = record.executionStatus === "submitted" || record.submitState === "submitted" || !!record.blockedReason || record.executionStatus === "cancelled";
    return {
      record, pkg, evaluation, responses, answers, progress: localProgress(evaluation), counts, readOnly,
      problems: localProblems(evaluation, responses), submit: submitOp ? { state: submitOp.state, error: submitOp.lastError, problems: submitOp.errors } : null,
      submitState: record.submitState, blockedReason: record.blockedReason,
    };
  }

  /** Save (or clear) one answer.  ``raw`` is the user's input in the answer contract's shape. */
  saveAnswer(assignmentId, itemCode, { raw, notApplicable = false, comment = "" }) {
    return this.serialized(async () => {
      const state = await this.open(assignmentId);
      if (!state) {
        throw new LocalConflict("This checklist is not prepared on this device.");
      }
      if (state.readOnly) {
        throw new LocalConflict("This checklist can no longer be edited here.");
      }
      const item = state.pkg.items.find((i) => i.code === itemCode);
      const result = state.evaluation.items[itemCode];
      if (!item || !result || !result.visible) {
        throw new LocalConflict("That question does not apply right now.");
      }
      let status;
      let value = null;
      if (notApplicable) {
        if (result.required) {
          return { ok: false, errors: [{ code: "not_applicable_not_allowed", message: "This question is required and cannot be marked not applicable." }] };
        }
        status = "not_applicable";
      } else if (isEmpty(item.answer_type, raw)) {
        status = "cleared";
      } else {
        const options = result.options ? result.options.available : undefined;
        const checked = normalize(item, raw, options);
        if (!checked.ok) {
          return { ok: false, errors: checked.errors };
        }
        status = "answered";
        value = checked.value;
      }
      const { batch, record } = await planResponseWrite(this.ctx, {
        assignmentId, versionId: state.record.versionId, itemId: item.id, status, value, comment, baseRevision: 0,
      });
      if (batch.length) {
        await this.ctx.storage.batch(batch); // resolves only after the transaction committed: only now is it "saved on this device"
      }
      return { ok: true, record, savedAt: this.ctx.now() };
    });
  }

  /** Queue the submission.  Not "submitted" until the server accepts it; the local check only avoids a pointless round trip. */
  submit(assignmentId) {
    return this.serialized(async () => {
      const state = await this.open(assignmentId);
      if (!state || state.readOnly) {
        throw new LocalConflict("This checklist can no longer be submitted from here.");
      }
      const found = state.problems;
      if (Object.keys(found).length) {
        return { ok: false, problems: found };
      }
      const { batch } = await planSubmit(this.ctx, { assignmentId, versionId: state.record.versionId });
      batch.push({ type: "put", store: STORES.packages, record: { ...state.record, submitState: "queued", submitProblems: null } });
      await this.ctx.storage.batch(batch);
      return { ok: true };
    });
  }
}
