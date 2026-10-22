/* In-memory implementation of the storage interface (database.js) - used by the Node tests so the sync engine, stores and conflict
 * handling can be exercised without a browser.  Semantics match IdbStorage: ``batch`` is atomic and values are copied on the way in
 * and out (no shared mutable references), and a failure injector lets tests simulate quota errors.
 */
import { STORES } from "./schema.js";
import { INDEXES, KEY_PATHS } from "./database-migrations.js";
import { StorageFailure } from "./database.js";

const clone = (value) => (value === undefined ? undefined : structuredClone(value));

export class MemoryStorage {
  constructor() {
    this.data = Object.fromEntries(Object.values(STORES).map((name) => [name, new Map()]));
    this.failNextWrite = null; // set to a StorageFailure to simulate a quota / transaction error
  }

  close() {}

  async get(store, key) {
    return clone(this.data[store].get(key));
  }

  async getAll(store) {
    return [...this.data[store].values()].map(clone);
  }

  async getAllByIndex(store, index, value) {
    const spec = (INDEXES[store] || []).find(([name]) => name === index);
    if (!spec) {
      throw new Error(`No index ${index} on ${store}`);
    }
    return [...this.data[store].values()].filter((r) => r[spec[1]] === value).map(clone);
  }

  async put(store, record) {
    return this.batch([{ type: "put", store, record }]);
  }

  async delete(store, key) {
    return this.batch([{ type: "delete", store, key }]);
  }

  async batch(ops) {
    if (this.failNextWrite) {
      const failure = this.failNextWrite;
      this.failNextWrite = null;
      throw failure;
    }
    const staged = Object.fromEntries(Object.keys(this.data).map((name) => [name, new Map(this.data[name])]));
    for (const op of ops) {
      if (op.type === "put") {
        const key = op.record[KEY_PATHS[op.store]];
        if (key === undefined) {
          throw new StorageFailure("storage", `Record for ${op.store} has no key.`);
        }
        staged[op.store].set(key, clone(op.record));
      } else {
        staged[op.store].delete(op.key);
      }
    }
    this.data = staged; // commit: all or nothing
  }
}
