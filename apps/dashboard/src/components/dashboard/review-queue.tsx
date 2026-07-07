"use client";

import { useCallback, useEffect, useState } from "react";
import { Inbox } from "lucide-react";

import { apiFetch } from "@/lib/api-client";
import { useDashboardData } from "@/lib/dashboard-data";
import { decidedBy, displayAction, displayReason, domainFromUrl, formatDateTime } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

type ReviewItem = {
  id: string;
  action: string;
  reason: string | null;
  details_json: { rationale?: string; confidence?: number } | null;
  url: string | null;
  title: string | null;
  domain: string | null;
  ts: number;
  child_id: string | null;
};

// Server-backed review queue: decisions from the last 7 days that deserve a
// guardian look and have no override yet. Every item can be resolved in one
// click — allow once, or mint a permanent rule for the whole site.
export function ReviewQueue({ childId }: { childId: string | null }) {
  const { token, selectedHousehold } = useDashboardData();
  const [items, setItems] = useState<ReviewItem[] | null>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const authToken = await token();
      const qs = childId ? `?child_id=${encodeURIComponent(childId)}` : "";
      const data = await apiFetch<{ items: ReviewItem[] }>(`/v1/review-queue${qs}`, authToken);
      setItems(data.items || []);
    } catch (e) {
      setError(String(e));
      setItems([]);
    }
  }, [token, childId]);

  useEffect(() => {
    void load();
  }, [load, selectedHousehold]);

  const run = async (item: ReviewItem, fn: (authToken: string | null) => Promise<void>) => {
    setBusy((previous) => ({ ...previous, [item.id]: true }));
    setError(null);
    try {
      const authToken = await token();
      await fn(authToken);
      setItems((previous) => (previous || []).filter((row) => row.id !== item.id));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy((previous) => {
        const next = { ...previous };
        delete next[item.id];
        return next;
      });
    }
  };

  const allowOnce = (item: ReviewItem) =>
    run(item, async (authToken) => {
      await apiFetch(`/v1/decisions/${encodeURIComponent(item.id)}/override`, authToken, {
        method: "POST",
        body: JSON.stringify({ action: "allow", reason: "review_queue" }),
      });
    });

  // "Always" mints a domain rule for this child; allow also overrides the
  // current decision so the effect is immediate, not just from the next visit.
  const alwaysRule = (item: ReviewItem, action: "allow" | "block") =>
    run(item, async (authToken) => {
      const domain = item.domain || domainFromUrl(item.url);
      await apiFetch("/v1/rules", authToken, {
        method: "POST",
        body: JSON.stringify({
          action,
          rule_type: "domain",
          pattern: domain,
          child_id: item.child_id,
          reason: "Added from review queue",
        }),
      });
      if (action === "allow") {
        await apiFetch(`/v1/decisions/${encodeURIComponent(item.id)}/override`, authToken, {
          method: "POST",
          body: JSON.stringify({ action: "allow", reason: "review_queue_rule" }),
        });
      }
    });

  const dismiss = (item: ReviewItem) =>
    setItems((previous) => (previous || []).filter((row) => row.id !== item.id));

  return (
    <Card>
      <CardHeader>
        <CardTitle>Review queue</CardTitle>
        <p className="text-sm text-muted-foreground">
          Restricted pages, uncertain checks, and anything waiting on a screenshot scan — resolved items disappear.
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {items === null && <p className="text-sm text-muted-foreground">Loading…</p>}
        {items !== null && items.length === 0 && (
          <div className="flex items-center gap-3 rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
            <Inbox className="size-5" />
            Nothing needs your attention right now.
          </div>
        )}
        {(items || []).map((item) => {
          const source = decidedBy(item.reason);
          const rationale = item.details_json?.rationale;
          return (
            <div key={item.id} className="space-y-2 rounded-lg border p-3">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={item.action === "block" ? "destructive" : "warning"}>{displayAction(item.action)}</Badge>
                {source && <Badge variant="outline">{source}</Badge>}
                <span className="text-xs text-muted-foreground">{formatDateTime(item.ts)}</span>
              </div>
              <p className="truncate text-sm font-medium">{item.title || domainFromUrl(item.url)}</p>
              <p className="truncate text-xs text-muted-foreground">{domainFromUrl(item.url)}</p>
              <p className="text-xs text-muted-foreground">{rationale || displayReason(item.reason)}</p>
              <div className="flex flex-wrap gap-2 pt-1">
                <Button size="sm" variant="outline" disabled={busy[item.id]} onClick={() => allowOnce(item)}>
                  Allow once
                </Button>
                <Button size="sm" variant="outline" disabled={busy[item.id]} onClick={() => alwaysRule(item, "allow")}>
                  Always allow site
                </Button>
                <Button size="sm" variant="destructive" disabled={busy[item.id]} onClick={() => alwaysRule(item, "block")}>
                  Always block site
                </Button>
                <Button size="sm" variant="ghost" disabled={busy[item.id]} onClick={() => dismiss(item)}>
                  Dismiss
                </Button>
              </div>
            </div>
          );
        })}
        {error && <p className="text-sm text-destructive">{error}</p>}
      </CardContent>
    </Card>
  );
}
