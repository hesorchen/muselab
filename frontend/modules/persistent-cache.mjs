// IndexedDB boundary for owner-scoped workspace snapshots.
//
// The cache is stale-while-revalidate only: frontend/app.js always confirms
// the stored cursor through /api/files/delta before treating it as current.
// Every failure is fail-soft so canonical HTTP remains the source of truth.

const DB_NAME = "muselab-persistent-cache-v1";
const DB_VERSION = 1;
const WORKSPACES = "workspaces";
let databasePromise;

function namespace(value) {
  const origin = typeof location !== "undefined" && location.origin
    ? location.origin : "local";
  return `${origin}\u0000${String(value || "")}`;
}

function openDatabase() {
  if (typeof indexedDB === "undefined") return Promise.resolve(null);
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(WORKSPACES)) {
        db.createObjectStore(WORKSPACES, { keyPath: "key" });
      }
    };
    request.onsuccess = () => {
      const db = request.result;
      const owningPromise = databasePromise;
      // Do not let an old tab pin schema version 1 forever. A future deploy can
      // upgrade the cache as soon as every open connection receives this event.
      // Clear the memoized promise too; otherwise this tab would keep returning
      // a closed IDBDatabase for every later cache operation.
      db.onversionchange = () => {
        if (databasePromise === owningPromise) databasePromise = undefined;
        db.close();
      };
      resolve(db);
    };
    request.onerror = () => reject(request.error || new Error("IndexedDB open failed"));
    request.onblocked = () => reject(new Error("IndexedDB open blocked"));
  });
}

function database() {
  if (!databasePromise) {
    databasePromise = openDatabase().catch(() => null);
  }
  return databasePromise;
}

export async function getWorkspaceSnapshot(owner) {
  if (!owner) return null;
  const db = await database();
  if (!db) return null;
  return new Promise(resolve => {
    const transaction = db.transaction(WORKSPACES, "readonly");
    const request = transaction.objectStore(WORKSPACES).get(namespace(owner));
    request.onsuccess = () => {
      const record = request.result;
      if (!record || !record.value || record.value.owner !== owner) {
        resolve(null);
        return;
      }
      resolve({ ...record.value, savedAt: record.savedAt });
    };
    request.onerror = () => resolve(null);
    transaction.onabort = () => resolve(null);
  });
}

export async function putWorkspaceSnapshot(owner, snapshot) {
  if (!owner || !snapshot) return false;
  const db = await database();
  if (!db) return false;
  return new Promise(resolve => {
    const transaction = db.transaction(WORKSPACES, "readwrite");
    transaction.objectStore(WORKSPACES).put({
      key: namespace(owner),
      savedAt: Date.now(),
      value: { ...snapshot, owner },
    });
    transaction.oncomplete = () => resolve(true);
    transaction.onerror = () => resolve(false);
    transaction.onabort = () => resolve(false);
  });
}

export async function deleteWorkspaceSnapshot(owner) {
  if (!owner) return false;
  const db = await database();
  if (!db) return false;
  return new Promise(resolve => {
    const transaction = db.transaction(WORKSPACES, "readwrite");
    transaction.objectStore(WORKSPACES).delete(namespace(owner));
    transaction.oncomplete = () => resolve(true);
    transaction.onerror = () => resolve(false);
    transaction.onabort = () => resolve(false);
  });
}
