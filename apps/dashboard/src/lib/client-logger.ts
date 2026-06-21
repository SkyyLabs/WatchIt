"use client";

type LogLevel = "debug" | "info" | "warn" | "error";

type ClientLogEntry = {
  ts: string;
  level: LogLevel;
  message: string;
  context?: Record<string, unknown>;
};

const LOG_KEY = "watchit.client.logs";
const MAX_ENTRIES = 500;

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
  const entry = {
    ts: new Date().toISOString(),
    level,
    message,
    context,
  };
  const consolePayload = context ? [message, context] : [message];

  if (level === "error") {
    console.error(...consolePayload);
  } else if (level === "warn") {
    console.warn(...consolePayload);
  } else {
    console.log(...consolePayload);
  }

  writeEntry(entry);
}

export const clientLogger = {
  debug: (message: string, context?: Record<string, unknown>) => emit("debug", message, context),
  info: (message: string, context?: Record<string, unknown>) => emit("info", message, context),
  warn: (message: string, context?: Record<string, unknown>) => emit("warn", message, context),
  error: (message: string, context?: Record<string, unknown>) => emit("error", message, context),
};

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
