"use client";

import { useCallback, useEffect, useState } from "react";
import { ShieldCheck, Trash2 } from "lucide-react";

import { apiFetch } from "@/lib/api-client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const HOUSEHOLD_SCOPE = "__household__";

type Rule = {
  id: string;
  child_id: string | null;
  device_id: string | null;
  action: "allow" | "block";
  rule_type: "domain" | "url" | "prefix";
  pattern: string;
  reason: string | null;
  expires_at: string | null;
};

type FormState = {
  action: "allow" | "block";
  ruleType: "domain" | "url";
  pattern: string;
  reason: string;
  scope: string; // HOUSEHOLD_SCOPE or the child id
};

type TestResult = { action: string; layer: string; detail: string };

const LAYER_LABEL: Record<string, string> = {
  rule: "your rule",
  schedule: "quiet hours",
  cache: "recent decision",
  policy: "built-in policy",
  ai: "AI review",
};

export function RulesManager({ childId, getToken }: { childId: string; getToken: () => Promise<string | null> }) {
  const [rules, setRules] = useState<Rule[]>([]);
  const [form, setForm] = useState<FormState>({ action: "block", ruleType: "domain", pattern: "", reason: "", scope: childId });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testUrl, setTestUrl] = useState("");
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [testing, setTesting] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const authToken = await getToken();
      const data = await apiFetch<{ rules: Rule[] }>(`/v1/rules?child_id=${encodeURIComponent(childId)}`, authToken);
      setRules(data.rules || []);
    } catch (e) {
      setError(String(e));
    }
  }, [childId, getToken]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    if (!form.pattern.trim()) {
      setError(form.ruleType === "domain" ? "Enter a domain (e.g. roblox.com)." : "Enter a full URL.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const authToken = await getToken();
      await apiFetch("/v1/rules", authToken, {
        method: "POST",
        body: JSON.stringify({
          action: form.action,
          rule_type: form.ruleType,
          pattern: form.pattern.trim(),
          child_id: form.scope === HOUSEHOLD_SCOPE ? null : form.scope,
          reason: form.reason.trim() || null,
        }),
      });
      setForm((prev) => ({ ...prev, pattern: "", reason: "" }));
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const runTest = async () => {
    if (!testUrl.trim()) {
      setError("Enter a URL to test.");
      return;
    }
    setTesting(true);
    setError(null);
    setTestResult(null);
    try {
      const authToken = await getToken();
      const result = await apiFetch<TestResult>("/v1/rules/test", authToken, {
        method: "POST",
        body: JSON.stringify({ url: testUrl.trim(), child_id: childId }),
      });
      setTestResult(result);
    } catch (e) {
      setError(String(e));
    } finally {
      setTesting(false);
    }
  };

  const remove = async (id: string) => {
    setBusy(true);
    setError(null);
    try {
      const authToken = await getToken();
      await apiFetch(`/v1/rules/${encodeURIComponent(id)}`, authToken, { method: "DELETE" });
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4 rounded-lg border bg-muted/30 p-4">
      <div className="flex items-center gap-2">
        <ShieldCheck className="size-4 text-muted-foreground" />
        <span className="font-medium">Allow / block rules</span>
        <span className="text-xs text-muted-foreground">
          Enforced instantly on the device — no AI wait. Device rules beat child rules; block beats allow.
        </span>
      </div>

      {rules.length > 0 && (
        <div className="space-y-2">
          {rules.map((rule) => (
            <div key={rule.id} className="flex flex-col gap-2 rounded-md border bg-background p-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <Badge variant={rule.action === "block" ? "destructive" : "success"}>{rule.action}</Badge>
                <Badge variant="outline">{rule.rule_type}</Badge>
                <Badge variant="secondary">{rule.child_id ? "this child" : "whole household"}</Badge>
                <span className="font-medium break-all">{rule.pattern}</span>
                {rule.reason && <span className="text-muted-foreground">{rule.reason}</span>}
              </div>
              <Button
                size="sm"
                variant="ghost"
                className="text-destructive hover:text-destructive"
                disabled={busy}
                onClick={() => remove(rule.id)}
                aria-label={`Delete rule for ${rule.pattern}`}
              >
                <Trash2 className="size-4" />
              </Button>
            </div>
          ))}
        </div>
      )}
      {rules.length === 0 && <p className="text-sm text-muted-foreground">No rules yet.</p>}

      <div className="space-y-3 rounded-md border bg-background p-3">
        <p className="text-sm font-medium">Add rule</p>
        <div className="grid gap-3 sm:grid-cols-4">
          <div className="space-y-1.5">
            <Label>Action</Label>
            <Select value={form.action} onValueChange={(value) => setForm((prev) => ({ ...prev, action: value as FormState["action"] }))}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="block">Block</SelectItem>
                <SelectItem value="allow">Allow</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>Type</Label>
            <Select value={form.ruleType} onValueChange={(value) => setForm((prev) => ({ ...prev, ruleType: value as FormState["ruleType"] }))}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="domain">Whole site (domain)</SelectItem>
                <SelectItem value="url">Exact page (URL)</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>Applies to</Label>
            <Select value={form.scope} onValueChange={(value) => setForm((prev) => ({ ...prev, scope: value }))}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value={childId}>This child</SelectItem>
                <SelectItem value={HOUSEHOLD_SCOPE}>Whole household</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={`rp-${childId}`}>{form.ruleType === "domain" ? "Domain" : "URL"}</Label>
            <Input
              id={`rp-${childId}`}
              placeholder={form.ruleType === "domain" ? "roblox.com" : "https://example.com/page"}
              value={form.pattern}
              onChange={(e) => setForm((prev) => ({ ...prev, pattern: e.target.value }))}
            />
          </div>
        </div>
        <div className="flex items-end gap-2">
          <div className="flex-1 space-y-1.5">
            <Label htmlFor={`rr-${childId}`}>Reason (optional, shown on the block page)</Label>
            <Input
              id={`rr-${childId}`}
              placeholder="Why this rule exists"
              value={form.reason}
              onChange={(e) => setForm((prev) => ({ ...prev, reason: e.target.value }))}
            />
          </div>
          <Button size="sm" disabled={busy} onClick={save}>Add rule</Button>
        </div>
      </div>

      <div className="space-y-3 rounded-md border bg-background p-3">
        <p className="text-sm font-medium">Test a URL</p>
        <p className="text-xs text-muted-foreground">
          See what would happen right now for this child — which rule, schedule, or cached decision applies.
        </p>
        <div className="flex items-end gap-2">
          <div className="flex-1 space-y-1.5">
            <Label htmlFor={`rt-${childId}`}>URL</Label>
            <Input
              id={`rt-${childId}`}
              placeholder="https://example.com/page"
              value={testUrl}
              onChange={(e) => setTestUrl(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void runTest(); }}
            />
          </div>
          <Button size="sm" disabled={testing} onClick={runTest}>Test</Button>
        </div>
        {testResult && (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge variant={testResult.action === "block" ? "destructive" : testResult.action === "allow" ? "success" : "secondary"}>
              {testResult.action === "unknown" ? "needs AI review" : testResult.action}
            </Badge>
            <Badge variant="outline">{LAYER_LABEL[testResult.layer] || testResult.layer}</Badge>
            <span className="text-muted-foreground">{testResult.detail}</span>
          </div>
        )}
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  );
}
