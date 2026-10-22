/* The IndexedDB repository: a deliberately small, promise-based storage interface (get / put / delete / getAll / getAllByIndex /
 * batch).  ``MemoryStorage`` (memory-store.js) implements the same interface for the Node tests.  Only what the checklist workflow
 * needs - this is not a general database framework.
 *
 * Failure model: a quota error becomes ``StorageFailure("quota")`` and any other transaction error ``StorageFailure("storage")`` so
 * the UI can say plainly that a write did NOT happen (the "Saved offline" label is only shown after ``batch`` resolves).
 */
import { DB_NAME, DB_VERSION } from "./schema.js";
import { KEY_PATHS, migrate } from "./database-migrations.js";

export class StorageFailure extends Error {
  constructor(kind, message, cause) {
    super(message);
    this.name = "StorageFailure";
    this.kind = kind; // "quota" | "blocked" | "storage" | "unavailable"
    this.cause = cause;
  }
}

function classify(error) {
  const name = error && error.name;
  if (name === "QuotaExceededError" || (error && error.code === 22)) {
    return new StorageFailure("quota", "This device has no storage space left. Free some space or sync and clear cached checklists.", error);
  }
  return new StorageFailure("storage", "The offline database could not be written.", error);
}

const wrap = (request) => new Promise((resolve, reject) => {
  request.onsuccess = () => resolve(request.result);
  request.onerror = () => reject(classify(request.error));
});

export class IdbStorage {
  constructor(db) {
    this.db = db;
    db.onversionchange = () => db.close(); // never hold an old version open: another tab is upgrading
  }

  static open(factory = globalThis.indexedDB, name = DB_NAME, version = DB_VERSION) {
    if (!factory) {
      return Promise.reject(new StorageFailure("unavailable", "This browser has no IndexedDB; offline mode is not available."));
    }
    return new Promise((resolve, reject) => {
      const request = factory.open(name, version);
      request.onupgradeneeded = (event) => migrate(request.result, request.transaction, event.oldVersion);
      request.onblocked = () => reject(new StorageFailure("blocked", "Close the other tabs of this application to finish updating offline storage."));
      request.onerror = () => reject(classify(request.error));
      request.onsuccess = () => resolve(new IdbStorage(request.result));
    });
  }

  close() {
    this.db.close();
  }

  async get(store, key) {
    return wrap(this.db.transaction(store, "readonly").objectStore(store).get(key));
  }

  async getAll(store) {
    return wrap(this.db.transaction(store, "readonly").objectStore(store).getAll());
  }

  async getAllByIndex(store, index, value) {
    return wrap(this.db.transaction(store, "readonly").objectStore(store).index(index).getAll(value));
  }

  async put(store, record) {
    return this.batch([{ type: "put", store, record }]);
  }

  async delete(store, key) {
    return this.batch([{ type: "delete", store, key }]);
  }

  /** Apply ``ops`` atomically: all of them or none (one IndexedDB transaction).  Resolves only after the transaction committed. */
  batch(ops) {
    const names = [...new Set(ops.map((o) => o.store))];
    return new Promise((resolve, reject) => {
      let tx;
      try {
        tx = this.db.transaction(names, "readwrite");
      } catch (error) {
        reject(classify(error));
        return;
      }
      tx.oncomplete = () => resolve();
      tx.onabort = () => reject(classify(tx.error));
      tx.onerror = () => reject(classify(tx.error));
      try {
        for (const op of ops) {
          const store = tx.objectStore(op.store);
          if (op.type === "put") {
            if (op.record[KEY_PATHS[op.store]] === undefined) {
              throw new StorageFailure("storage", `Record for ${op.store} has no key.`);
            }
            store.put(op.record);
          } else {
            store.delete(op.key);
          }
        }
      } catch (error) {
        try { tx.abort(); } catch (ignored) { /* already finished */ }
        reject(error instanceof StorageFailure ? error : classify(error));
      }
    });
  }
}

export const openStorage = (...args) => IdbStorage.open(...args);
