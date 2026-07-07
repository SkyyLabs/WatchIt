export function formatTime(value: unknown): string {
  const ms = Number(value || Date.now());
  if (!Number.isFinite(ms)) return "Unknown";
  return new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function formatDateTime(value: unknown): string {
  const ms = Number(value || Date.now());
  if (!Number.isFinite(ms)) return "Unknown";
  return new Date(ms).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function formatCountdown(ms: number): string {
  if (ms <= 0) return "0s";
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export function domainFromUrl(url?: string | null): string {
  if (!url) return "Unknown site";
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host.startsWith("www.") ? host.slice(4) : host;
  } catch {
    return url.replace(/^https?:\/\//, "").split("/")[0] || "Unknown site";
  }
}

export function displayAction(action?: string): string {
  const labels: Record<string, string> = {
    allow: "Safe",
    warn: "Needs attention",
    blur: "Hidden",
    block: "Blocked",
    notify: "Parent notified",
    pending: "Checking",
  };
  return labels[action || ""] || "Unknown";
}

export function displayReason(reason?: string | null): string {
  if (!reason) return "No reason recorded";
  if (reason === "pending_ocr") return "Needs screenshot scan";
  if (reason.startsWith("llm:")) return "AI safety review";
  if (reason === "manual_override") return "Parent changed this decision";
  if (reason === "default allow") return "No risk detected";
  if (reason === "prefilter high") return "High-risk page signals";
  if (reason === "system_uncertain") return "Safety check unavailable — page cautioned";
  if (reason.startsWith("manual_rule:")) return "Your rule";
  return reason.replace(/_/g, " ");
}

// Which layer decided — answers "why did WatchIt act" at a glance.
export function decidedBy(reason?: string | null): string | null {
  if (!reason) return null;
  if (reason.startsWith("manual_rule:")) return "Rule";
  if (reason === "manual_override") return "Parent";
  if (reason === "quiet hours") return "Schedule";
  if (reason === "url_cache" || reason === "cached decision") return "Cache";
  if (reason.startsWith("llm:") || reason === "pending_ocr") return "AI";
  if (reason === "system_uncertain") return "System";
  if (reason.startsWith("allowlist") || reason.startsWith("blocklist") || reason === "prefilter high" || reason.startsWith("headline")) return "Policy";
  return null;
}
