// Firebase sync disabled — IndexedDB only

const DB_NAME = "chronos_sessions";
const DB_VERSION = 1;
const STORE = "sessions";

export interface StoredBeat {
  beatIndex: number;
  beatLabel: string;
  visual: { type: "video" | "image"; mimeType: string; base64: string } | null;
  subtitles: string[];
  narrationChunks: string[];
  durationSeconds: number;
}

export interface StoredSession {
  id: string;
  topic: string;
  docTitle: string;
  date: string;
  totalBeats: number;
  beats: StoredBeat[];
  bgmBase64: string | null;
  quiz: Array<{
    question: string;
    options: string[];
    correct: string;
    explanation: string;
  }> | null;
}

// ── IndexedDB helpers ────────────────────────────────────────────────────────

function openDB(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE))
        req.result.createObjectStore(STORE, { keyPath: "id" });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbPut(session: StoredSession): Promise<void> {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(session);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function idbGet(id: string): Promise<StoredSession | null> {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const req = tx.objectStore(STORE).get(id);
    req.onsuccess = () => resolve((req.result as StoredSession) ?? null);
    req.onerror = () => reject(req.error);
  });
}

async function idbDelete(id: string): Promise<void> {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

// ── Public API ───────────────────────────────────────────────────────────────

export async function saveSession(session: StoredSession): Promise<void> {
  await idbPut(session);
  // Firebase sync disabled
}

export async function loadSession(id: string): Promise<StoredSession | null> {
  return await idbGet(id);
}

export async function deleteSession(id: string): Promise<void> {
  await idbDelete(id);
}

/** List all sessions from IndexedDB only (Firebase disabled). */
export async function listSessions(): Promise<Array<{ id: string; topic: string; docTitle: string; date: string; quiz: any }>> {
  return [];
}
