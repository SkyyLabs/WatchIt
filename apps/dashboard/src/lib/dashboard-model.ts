import { domainFromUrl } from "./format";

export type TimeRange = "today" | "7d" | "30d";
export type DashboardView = "dashboard" | "settings" | "profile" | "children";
export type DecisionAction = "allow" | "warn" | "blur" | "block" | "notify" | string;

export type ChildProfile = {
  id: string;
  household_id?: string;
  name?: string;
  timezone?: string;
  strictness?: "lenient" | "standard" | "strict" | string;
  age?: number;
  status?: string;
  monitoring_active?: boolean;
  active_device_count?: number;
};

export type SecuritySettings = {
  parent_pin_set: boolean;
  pin_policy: {
    min_length: number;
    max_length: number;
    digits_only: boolean;
  };
};

export type EventRecord = {
  id?: string;
  child_id?: string;
  ts?: number;
  kind?: string;
  url?: string;
  normalized_url?: string;
  domain?: string;
  title?: string;
};

export type DecisionRecord = {
  id: string;
  event_id?: string;
  child_id?: string;
  action: DecisionAction;
  reason?: string;
  details_json?: Record<string, unknown>;
  categories?: string[];
  original_action?: string;
  manual_action?: string;
  manual_flagged?: boolean;
  manual_processed?: boolean;
  manual_updated_at?: number;
  ts?: number;
  url?: string;
  title?: string;
};

export type AttentionItem = DecisionRecord & {
  riskLevel: "high" | "medium" | "low";
  priority: number;
};

export type DashboardMetrics = {
  safetyScore: number | null;
  sitesChecked: number;
  flaggedCount: number;
  overrideCount: number;
  latestRisk: DecisionRecord | null;
};

const RISK_ACTIONS = new Set(["warn", "blur", "block", "notify"]);
const ACTION_SCORE: Record<string, number> = {
  allow: 100,
  notify: 85,
  warn: 75,
  blur: 60,
  block: 45,
};

export function normalizeDecision(data: any): DecisionRecord {
  const categories = data?.categories || data?.details_json?.categories || [];
  return {
    ...data,
    categories: Array.isArray(categories) ? categories : [],
    id: data?.id || data?.decision_id || data?.event_id || crypto.randomUUID(),
    manual_flagged: Boolean(data?.manual_flagged),
  };
}

export function rangeStart(range: TimeRange): number {
  const now = new Date();
  if (range === "today") {
    const start = new Date(now);
    start.setHours(0, 0, 0, 0);
    return start.getTime();
  }
  const days = range === "7d" ? 7 : 30;
  return Date.now() - days * 24 * 60 * 60 * 1000;
}

export function filterByChildAndRange<T extends { child_id?: string; ts?: number }>(
  rows: T[],
  childId: string | null,
  range: TimeRange,
): T[] {
  const start = rangeStart(range);
  return rows.filter((row) => {
    const ts = Number(row.ts || 0);
    const childOk = !childId || row.child_id === childId;
    return childOk && ts >= start;
  });
}

export function computeMetrics(decisions: DecisionRecord[], events: EventRecord[]): DashboardMetrics {
  const scored = decisions.map((decision) => ACTION_SCORE[decision.action] ?? 80);
  const safetyScore = scored.length
    ? Math.round(scored.reduce((sum, score) => sum + score, 0) / scored.length)
    : null;
  const sites = new Set<string>();
  for (const event of events) sites.add(event.domain || domainFromUrl(event.url));
  for (const decision of decisions) sites.add(domainFromUrl(decision.url));
  const risky = decisions.filter((decision) => RISK_ACTIONS.has(decision.action));
  return {
    safetyScore,
    sitesChecked: Array.from(sites).filter(Boolean).length,
    flaggedCount: risky.length,
    overrideCount: decisions.filter((decision) => decision.manual_flagged).length,
    latestRisk: risky[0] || null,
  };
}

export function attentionItems(decisions: DecisionRecord[]): AttentionItem[] {
  return decisions
    .filter((decision) => {
      const categories = decision.categories || [];
      return (
        RISK_ACTIONS.has(decision.action) ||
        decision.reason === "pending_ocr" ||
        categories.some((category) => /adult|risk|harm|bully|violence|headline|schedule/i.test(category))
      );
    })
    .map((decision) => {
      const priority = decision.action === "block" ? 100 : decision.action === "blur" ? 80 : decision.action === "warn" ? 60 : 40;
      const riskLevel: AttentionItem["riskLevel"] = priority >= 90 ? "high" : priority >= 60 ? "medium" : "low";
      return {
        ...decision,
        priority,
        riskLevel,
      };
    })
    .sort((a, b) => b.priority - a.priority || Number(b.ts || 0) - Number(a.ts || 0))
    .slice(0, 8);
}

export function categoryBreakdown(decisions: DecisionRecord[], events: EventRecord[]) {
  const counts: Record<string, number> = {
    Educational: 0,
    Entertainment: 0,
    Social: 0,
    Search: 0,
    "Risky / blocked": 0,
    Unknown: 0,
  };

  for (const decision of decisions) {
    if (["warn", "blur", "block", "notify"].includes(decision.action)) {
      counts["Risky / blocked"] += 1;
      continue;
    }
    incrementCategory(counts, decision.url, decision.title, decision.categories);
  }
  for (const event of events) {
    incrementCategory(counts, event.url, event.title, []);
  }

  const total = Object.values(counts).reduce((sum, count) => sum + count, 0) || 1;
  return Object.entries(counts).map(([label, count]) => ({
    label,
    count,
    percent: Math.round((count / total) * 100),
  }));
}

function incrementCategory(
  counts: Record<string, number>,
  url?: string,
  title?: string,
  categories: string[] = [],
) {
  const text = `${domainFromUrl(url)} ${title || ""} ${categories.join(" ")}`.toLowerCase();
  if (/school|edu|learn|khan|wikipedia|course|study/.test(text)) counts.Educational += 1;
  else if (/youtube|netflix|game|music|video|twitch|entertain/.test(text)) counts.Entertainment += 1;
  else if (/instagram|tiktok|facebook|discord|reddit|snap|social/.test(text)) counts.Social += 1;
  else if (/google|bing|search|duckduckgo/.test(text)) counts.Search += 1;
  else counts.Unknown += 1;
}

export function makeChildId(name: string): string {
  const slug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return slug ? `child_${slug}` : "";
}
