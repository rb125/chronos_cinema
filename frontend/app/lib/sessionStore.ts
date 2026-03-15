/**
 * sessionStore — IndexedDB (local cache) + Firebase (persistent cloud sync).
 *
 * Layout:
 *   Firestore:  sessions/{id}  → metadata + subtitles + quiz (small data)
 *   Storage:    sessions/{id}/beat_{n}_audio.b64   → narration PCM chunks (joined)
 *               sessions/{id}/beat_{n}_visual.b64  → image/video base64
 *               sessions/{id}/bgm.b64              → BGM audio
 *   IndexedDB:  full session cached locally after first load
 */

import {
  doc, setDoc, getDoc, getDocs, collection, deleteDoc, serverTimestamp,
} from "firebase/firestore";
import {
  ref, uploadString, getDownloadURL, deleteObject, listAll,
} from "firebase/storage";
import { db, storage } from "./firebase";

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

// ── Firebase helpers ─────────────────────────────────────────────────────────

async function fbUploadBlob(path: string, data: string): Promise<void> {
  await uploadString(ref(storage, path), data);
}

async function fbDownloadBlob(path: string): Promise<string | null> {
  try {
    const url = await getDownloadURL(ref(storage, path));
    const res = await fetch(url);
    return await res.text();
  } catch {
    return null;
  }
}

// ── Public API ───────────────────────────────────────────────────────────────

export async function saveSession(session: StoredSession): Promise<void> {
  // 1. Save locally immediately
  await idbPut(session);

  // 2. Upload blobs to Storage in parallel
  const uploads: Promise<void>[] = [];
  const prefix = `sessions/${session.id}`;

  for (const beat of session.beats) {
    if (beat.visual?.base64) {
      uploads.push(fbUploadBlob(`${prefix}/beat_${beat.beatIndex}_visual.b64`, beat.visual.base64));
    }
    if (beat.narrationChunks.length > 0) {
      // Join chunks with a separator so we can split on load
      uploads.push(fbUploadBlob(`${prefix}/beat_${beat.beatIndex}_audio.b64`, beat.narrationChunks.join("|")));
    }
  }
  if (session.bgmBase64) {
    uploads.push(fbUploadBlob(`${prefix}/bgm.b64`, session.bgmBase64));
  }

  await Promise.allSettled(uploads);

  // 3. Save metadata (no blobs) to Firestore
  const meta = {
    id: session.id,
    topic: session.topic,
    docTitle: session.docTitle,
    date: session.date,
    totalBeats: session.totalBeats,
    createdAt: serverTimestamp(),
    beats: session.beats.map((b) => ({
      beatIndex: b.beatIndex,
      beatLabel: b.beatLabel,
      subtitles: b.subtitles,
      durationSeconds: b.durationSeconds,
      hasVisual: !!b.visual,
      visualType: b.visual?.type ?? null,
      visualMimeType: b.visual?.mimeType ?? null,
      hasAudio: b.narrationChunks.length > 0,
    })),
    quiz: session.quiz ?? null,
  };

  await setDoc(doc(db, "sessions", session.id), meta);
}

export async function loadSession(id: string): Promise<StoredSession | null> {
  // 1. Try local cache first
  const cached = await idbGet(id);
  if (cached) return cached;

  // 2. Load from Firebase
  const snap = await getDoc(doc(db, "sessions", id));
  if (!snap.exists()) return null;

  const meta = snap.data();
  const prefix = `sessions/${id}`;

  // 3. Download blobs in parallel
  const beats: StoredBeat[] = await Promise.all(
    (meta.beats as any[]).map(async (b) => {
      const [visualB64, audioJoined] = await Promise.all([
        b.hasVisual ? fbDownloadBlob(`${prefix}/beat_${b.beatIndex}_visual.b64`) : Promise.resolve(null),
        b.hasAudio ? fbDownloadBlob(`${prefix}/beat_${b.beatIndex}_audio.b64`) : Promise.resolve(null),
      ]);
      return {
        beatIndex: b.beatIndex,
        beatLabel: b.beatLabel,
        subtitles: b.subtitles ?? [],
        durationSeconds: b.durationSeconds ?? 0,
        narrationChunks: audioJoined ? audioJoined.split("|") : [],
        visual: visualB64 && b.visualType
          ? { type: b.visualType, mimeType: b.visualMimeType, base64: visualB64 }
          : null,
      };
    })
  );

  const bgmBase64 = await fbDownloadBlob(`${prefix}/bgm.b64`);

  const session: StoredSession = {
    id: meta.id,
    topic: meta.topic,
    docTitle: meta.docTitle,
    date: meta.date,
    totalBeats: meta.totalBeats,
    beats,
    bgmBase64,
    quiz: meta.quiz ?? null,
  };

  // Cache locally for next time
  await idbPut(session);
  return session;
}

export async function deleteSession(id: string): Promise<void> {
  await idbDelete(id);
  await deleteDoc(doc(db, "sessions", id));
  // Delete storage blobs
  try {
    const listResult = await listAll(ref(storage, `sessions/${id}`));
    await Promise.allSettled(listResult.items.map((item) => deleteObject(item)));
  } catch {}
}

/** List all sessions from Firestore (for cross-device history). */
export async function listSessions(): Promise<Array<{ id: string; topic: string; docTitle: string; date: string; quiz: any }>> {
  const snap = await getDocs(collection(db, "sessions"));
  return snap.docs.map((d) => {
    const data = d.data();
    return { id: data.id, topic: data.topic, docTitle: data.docTitle, date: data.date, quiz: data.quiz };
  }).sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime());
}
