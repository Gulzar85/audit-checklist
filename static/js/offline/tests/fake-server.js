/* A small in-process stand-in for the server's offline protocol (apps/executions/offline/sync.py): per-user receipts keyed by operation
 * id with payload fingerprints, per-item revisions, conflicts on a stale base, submit blocked after a failure.  The Python tests prove
 * the real server behaves this way; these tests prove the client reacts correctly to it.
 */
import { newId } from "../schema.js";

export const A = "11111111-1111-4111-8111-111111111111"; // assignment
export const V = "22222222-2222-4222-8222-222222222222"; // version
export const ITEMS = { exit: "33333333-3333-4333-8333-333333333331", temp: "33333333-3333-4333-8333-333333333332", why: "33333333-3333-4333-8333-333333333333" };
export const USER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
export const ORG = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
export const ACCOUNT = { userId: USER, organizationId: ORG };
export const NS = `${USER}:${ORG}`;

export function fixturePackage(overrides = {}) {
  return {
    schema_version: 1, evaluator_version: 1, package_hash: "a".repeat(64), generated_at: "2026-10-22T10:00:00Z", execution: null,
    assignment: { id: A, reference: "ASG-00001", status: "assigned", priority: "normal", due_at: null, instructions: "", organization: ORG, location: { id: newId(), name: "R1", path: "North / R1" } },
    checklist: { template: newId(), name: "Daily", code: "D", version_id: V, version_number: 1, integrity_hash: "x" },
    sections: [{ id: newId(), code: "S1", name: "Safety", description: "", instructions: "", sort_order: 1 }],
    items: [
      { id: ITEMS.exit, code: "EXIT", section: "S1", question: "Exit accessible?", help_text: "", instructions: "", answer_type: "yes_no", config: {}, required: true, sort_order: 1, options: [] },
      { id: ITEMS.temp, code: "TEMP", section: "S1", question: "Temperature?", help_text: "", instructions: "", answer_type: "numeric", config: { min: 0, max: 10, decimals: 1 }, required: true, sort_order: 2, options: [] },
      { id: ITEMS.why, code: "WHY", section: "S1", question: "Why?", help_text: "", instructions: "", answer_type: "long_text", config: { min_length: 0, max_length: 5000 }, required: false, sort_order: 3, options: [] },
    ],
    rules: [{ code: "R1", name: "r", condition: { source_item_code: "EXIT", comparison: "EQUALS", value: "no" },
      actions: [{ type: "SHOW_ITEM", target: { type: "item", code: "WHY" } }, { type: "REQUIRE_ITEM", target: { type: "item", code: "WHY" } }] }],
    ...overrides,
  };
}

const ok = (data, status = 200) => ({ ok: status < 400, status, headers: { get: () => null }, json: async () => ({ data }) });
const err = (status, error = {}) => ({ ok: false, status, headers: { get: () => null }, json: async () => ({ error }) });

export class FakeServer {
  constructor(pkg = fixturePackage()) {
    this.pkg = pkg;
    this.rows = new Map(); // item id -> {value, revision, comment}
    this.receipts = new Map(); // op id -> {fp, result}
    this.submitted = false;
    this.requests = [];
    this.session = { user_id: USER, organization_id: ORG, csrf_token: "tok" };
    this.down = false; // network failure before the server sees the request
    this.dropResponse = false; // the server processes the request but the response never arrives
    this.status = null; // force an HTTP status
  }

  fetch = async (url, init = {}) => {
    this.requests.push({ url, init });
    if (this.down) {
      throw new TypeError("network down");
    }
    if (url.endsWith("/health/")) {
      return ok({ status: "ok" });
    }
    if (url.endsWith("/offline/session/")) {
      return this.session ? ok(this.session) : err(401);
    }
    if (/\/package\/$/.test(url)) {
      return ok(this.pkg);
    }
    if (url.endsWith("/offline/sync/")) {
      if (this.status) {
        return err(this.status);
      }
      const body = JSON.parse(init.body);
      const results = [];
      const failed = new Set();
      for (const op of [...body.operations].sort((a, b) => a.seq - b.seq)) {
        results.push(this.process(op, failed));
      }
      const byOrder = body.operations.map((op) => results.find((r) => r.operation_id === op.operation_id));
      if (this.dropResponse) {
        throw new TypeError("connection reset");
      }
      return ok({ schema_version: 1, results: byOrder });
    }
    if (url.endsWith("/offline/resolve-conflict/")) {
      const body = JSON.parse(init.body);
      const row = this.rows.get(body.item_id);
      if (body.resolution === "keep_server") {
        return ok({ operation_id: body.operation_id, status: "applied", resolution: "keep_server", server: { exists: !!row, status: "answered", value: row && row.value, comment: (row && row.comment) || "", revision: row ? row.revision : 0 } });
      }
      const r = this.process({ operation_id: body.operation_id, type: "upsert_response", item_id: body.item_id, value: body.value, not_applicable: body.not_applicable, comment: body.comment, base_revision: body.base_revision, assignment_id: A, seq: 0 }, new Set());
      return ok({ ...r, resolution: "use_local" });
    }
    throw new Error(`unexpected ${url}`);
  };

  process(op, failed) {
    const fp = JSON.stringify([op.type, op.item_id, op.value, op.not_applicable, op.comment, op.base_revision]);
    const prior = this.receipts.get(op.operation_id);
    if (prior) {
      return { operation_id: op.operation_id, ...prior.result, duplicate: true };
    }
    let result;
    if (op.type === "submit") {
      result = failed.size ? { status: "blocked", code: "dependency_failed" } : this.submitted ? { status: "applied", already_submitted: true } : (this.submitted = true, { status: "applied", execution_status: "submitted", execution: "e".repeat(8) + "-0000-4000-8000-000000000000" });
    } else if (this.submitted) {
      result = { status: "conflict", code: "execution_submitted", message: "already submitted", server: this.snapshot(op.item_id) };
    } else {
      const row = this.rows.get(op.item_id);
      const revision = row ? row.revision : 0;
      if ((op.base_revision || 0) !== revision) {
        result = { status: "conflict", code: "revision_mismatch", message: "changed elsewhere", server: this.snapshot(op.item_id) };
      } else if (op.item_id === ITEMS.temp && op.type === "upsert_response" && Number(op.value) > 10) {
        result = { status: "rejected", code: "validation_failed", errors: [{ code: "out_of_range", message: "too high" }] };
      } else if (op.type === "clear_response") {
        this.rows.delete(op.item_id);
        result = { status: "applied", response_revision: 0, execution: "e".repeat(8) + "-0000-4000-8000-000000000000" };
      } else {
        this.rows.set(op.item_id, { value: op.value, comment: op.comment, revision: revision + 1 });
        result = { status: "applied", response_revision: revision + 1, execution: "e".repeat(8) + "-0000-4000-8000-000000000000" };
      }
    }
    if (result.status !== "applied") {
      failed.add(op.operation_id);
    }
    this.receipts.set(op.operation_id, { fp, result });
    return { operation_id: op.operation_id, ...result };
  }

  snapshot(itemId) {
    const row = this.rows.get(itemId);
    return row ? { exists: true, status: "answered", value: row.value, comment: row.comment, revision: row.revision } : { exists: false, revision: 0 };
  }

  /** Another device changes an answer on the server. */
  edit(itemId, value) {
    const row = this.rows.get(itemId);
    this.rows.set(itemId, { value, comment: "", revision: (row ? row.revision : 0) + 1 });
  }
}
