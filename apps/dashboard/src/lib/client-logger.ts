"use client";

import { API_BASE_URL } from "@/lib/api-client";

type LogLevel = "debug" | "info" | "warn" | "error";

type ClientLogEntry = {
  ts: string;
  level: LogLevel;
  message: string;
  context?: Record<string, unknown>;
};

const LOG_KEY = "watchit.client.logs";
const MAX_ENTRIES = 500;
const MAX_PENDING = 500;
const FLUSH_BATCH = 100;
const FLUSH_INTERVAL_MS = 5000;

// Unsent entries awaiting a flush to the backend. Kept separate from the
// localStorage ring buffer, which is a full backup for the "Export logs" button.
let pending: ClientLogEntry[] = [];
let tokenGetter: (() => Promise<string | null>) | null = null;

function readEntries(): ClientLogEntry[] {
  try {
    const raw = window.localStorage.getItem(LOG_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function writeEntry(entry: ClientLogEntry) {
  try {
    const entries = [...readEntries(), entry].slice(-MAX_ENTRIES);
    window.localStorage.setItem(LOG_KEY, JSON.stringify(entries));
  } catch {
    // Storage failure should not affect the dashboard.
  }
}

function emit(level: LogLevel, message: string, context?: Record<string, unknown>) {
  const entry: ClientLogEntry = {
    ts: new Date().toISOString(),
    level,
    message,
    context,
  };
  // No console output: logs land in localStorage (backup) and ship to the
  // backend for the log drain. emit() stays synchronous and cheap; the network
  // send happens out-of-band in flush().
  writeEntry(entry);
  pending.push(entry);
  if (pending.length > MAX_PENDING) {
    pending = pending.slice(-MAX_PENDING);
  }
}

export const clientLogger = {
  debug: (message: string, context?: Record<string, unknown>) => emit("debug", message, context),
  info: (message: string, context?: Record<string, unknown>) => emit("info", message, context),
  warn: (message: string, context?: Record<string, unknown>) => emit("warn", message, context),
  error: (message: string, context?: Record<string, unknown>) => emit("error", message, context),
};

async function flush(): Promise<void> {
  if (!tokenGetter || pending.length === 0) return;
  const batch = pending.slice(0, FLUSH_BATCH);
  pending = pending.slice(batch.length);
  try {
    const token = await tokenGetter();
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (token) headers.authorization = `Bearer ${token}`;
    const res = await fetch(`${API_BASE_URL}/v1/client-logs`, {
      method: "POST",
      headers,
      body: JSON.stringify({ entries: batch }),
      keepalive: true,
    });
    if (!res.ok) throw new Error(`client-logs ${res.status}`);
  } catch {
    // Re-queue on failure so entries aren't lost; capped by MAX_PENDING.
    pending = [...batch, ...pending].slice(-MAX_PENDING);
  }
}

/**
 * Begin shipping buffered client logs to the backend. Call once from a client
 * component that has the Clerk token getter (e.g. the dashboard provider).
 * Returns a cleanup function that stops the timer and listeners.
 */
export function startClientLogShipping(getToken: () => Promise<string | null>): () => void {
  tokenGetter = getToken;
  const timer = window.setInterval(() => void flush(), FLUSH_INTERVAL_MS);
  const onHide = () => void flush();
  window.addEventListener("visibilitychange", onHide);
  window.addEventListener("pagehide", onHide);
  return () => {
    window.clearInterval(timer);
    window.removeEventListener("visibilitychange", onHide);
    window.removeEventListener("pagehide", onHide);
    tokenGetter = null;
  };
}

export function downloadClientLogFile() {
  const entries = readEntries();
  const jsonl = entries.map((entry) => JSON.stringify(entry)).join("\n");
  const blob = new Blob([jsonl + (jsonl ? "\n" : "")], { type: "application/jsonl" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `watchit-client-${new Date().toISOString().replace(/[:.]/g, "-")}.jsonl`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
