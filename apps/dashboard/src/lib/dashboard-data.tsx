"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";

import { apiFetch, decisionStreamUrl } from "@/lib/api-client";
import { clientLogger } from "@/lib/client-logger";
import {
  ChildProfile,
  DecisionRecord,
  EventRecord,
  normalizeDecision,
  SecuritySettings,
} from "@/lib/dashboard-model";

type DashboardData = {
  isLoaded: boolean;
  isSignedIn: boolean;
  token: () => Promise<string | null>;

  children: ChildProfile[];
  activeChildId: string | null;
  selectedChild: string | null;
  setSelectedChild: (id: string) => void;
  security: SecuritySettings | null;
  setSecurity: React.Dispatch<React.SetStateAction<SecuritySettings | null>>;
  decisions: DecisionRecord[];
  setDecisions: React.Dispatch<React.SetStateAction<DecisionRecord[]>>;
  events: EventRecord[];

  coreLoading: boolean;
  activityLoading: boolean;
  error: string | null;

  refreshCore: () => Promise<void>;
  ensureActivity: () => void;
};

// Fetched data + SSE live here, above the route pages, so navigating between
// /, /settings, /profile, /children reuses the same data instead of remounting
// and refetching everything on every click.
const DashboardDataContext = createContext<DashboardData | null>(null);

// Activity (decisions + events) is fetched lazily and capped small — only the
// dashboard view renders it, and the default "today" range needs few rows.
const ACTIVITY_LIMIT = 50;

export function DashboardDataProvider({ children }: { children: React.ReactNode }) {
  const { isLoaded, isSignedIn, getToken } = useAuth();
  const token = useCallback(() => getToken(), [getToken]);

  const [childList, setChildList] = useState<ChildProfile[]>([]);
  const [activeChildId, setActiveChildId] = useState<string | null>(null);
  const [selectedChild, setSelectedChild] = useState<string | null>(null);
  const [security, setSecurity] = useState<SecuritySettings | null>(null);
  const [decisions, setDecisions] = useState<DecisionRecord[]>([]);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [coreLoading, setCoreLoading] = useState(true);
  const [activityLoading, setActivityLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const activityRequested = useRef(false);

  const refreshCore = useCallback(async () => {
    if (!isSignedIn) return;
    setError(null);
    setCoreLoading(true);
    try {
      const authToken = await token();
      const [childrenResp, securityResp] = await Promise.all([
        apiFetch<{ children: ChildProfile[]; active_child_id: string | null }>("/v1/children", authToken),
        apiFetch<SecuritySettings>("/v1/settings/security", authToken),
      ]);
      const list = childrenResp.children || [];
      setChildList(list);
      setActiveChildId(childrenResp.active_child_id || null);
      setSelectedChild((previous) => {
        if (!list.length) return null;
        if (childrenResp.active_child_id && list.some((child) => child.id === childrenResp.active_child_id)) {
          return childrenResp.active_child_id;
        }
        if (previous && list.some((child) => child.id === previous)) return previous;
        return list[0].id;
      });
      setSecurity(securityResp);
      clientLogger.info("dashboard core loaded", { children: list.length, parent_pin_set: securityResp.parent_pin_set });
    } catch (err) {
      setError("Could not load dashboard data. Check the API service and try again.");
      clientLogger.error("failed to load dashboard core", { error: String(err) });
    } finally {
      setCoreLoading(false);
    }
  }, [isSignedIn, token]);

  const refreshActivity = useCallback(async () => {
    if (!isSignedIn) return;
    setActivityLoading(true);
    try {
      const authToken = await token();
      const [decisionsResp, eventsResp] = await Promise.all([
        apiFetch<{ decisions: DecisionRecord[] }>(`/v1/decisions?limit=${ACTIVITY_LIMIT}`, authToken),
        apiFetch<{ events: EventRecord[] }>(`/v1/events?limit=${ACTIVITY_LIMIT}`, authToken),
      ]);
      setDecisions((decisionsResp.decisions || []).map(normalizeDecision));
      setEvents(eventsResp.events || []);
      clientLogger.info("dashboard activity loaded", {
        decisions: decisionsResp.decisions?.length || 0,
        events: eventsResp.events?.length || 0,
      });
    } catch (err) {
      clientLogger.error("failed to load dashboard activity", { error: String(err) });
    } finally {
      setActivityLoading(false);
    }
  }, [isSignedIn, token]);

  const ensureActivity = useCallback(() => {
    if (activityRequested.current) return;
    activityRequested.current = true;
    void refreshActivity();
  }, [refreshActivity]);

  useEffect(() => {
    if (!isSignedIn) {
      setChildList([]);
      setDecisions([]);
      setEvents([]);
      setSelectedChild(null);
      setSecurity(null);
      activityRequested.current = false;
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
      return;
    }
    refreshCore();
  }, [isSignedIn, refreshCore]);

  useEffect(() => {
    if (!isSignedIn) return;
    let cancelled = false;
    let es: EventSource | null = null;
    token()
      .then((authToken) => {
        if (cancelled) return;
        es = new EventSource(decisionStreamUrl(authToken));
        es.onmessage = (event) => {
          try {
            const msg = normalizeDecision(JSON.parse(event.data));
            setDecisions((previous) => {
              const index = previous.findIndex((decision) => decision.id === msg.id);
              if (index >= 0) {
                const clone = [...previous];
                clone[index] = { ...clone[index], ...msg };
                return clone;
              }
              return [msg, ...previous].slice(0, ACTIVITY_LIMIT);
            });
          } catch (err) {
            clientLogger.error("failed to parse decision stream message", { error: String(err) });
          }
        };
        es.onerror = () => clientLogger.warn("decision stream error");
        esRef.current = es;
      })
      .catch((err) => clientLogger.error("failed to open decision stream", { error: String(err) }));
    return () => {
      cancelled = true;
      if (es) es.close();
    };
  }, [isSignedIn, token]);

  const value: DashboardData = {
    isLoaded,
    isSignedIn,
    token,
    children: childList,
    activeChildId,
    selectedChild,
    setSelectedChild,
    security,
    setSecurity,
    decisions,
    setDecisions,
    events,
    coreLoading,
    activityLoading,
    error,
    refreshCore,
    ensureActivity,
  };

  return <DashboardDataContext.Provider value={value}>{children}</DashboardDataContext.Provider>;
}

export function useDashboardData(): DashboardData {
  const ctx = useContext(DashboardDataContext);
  if (!ctx) throw new Error("useDashboardData must be used within DashboardDataProvider");
  return ctx;
}
