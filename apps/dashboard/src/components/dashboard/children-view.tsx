"use client";

import { useState } from "react";
import { apiFetch } from "@/lib/api-client";
import { QuietHours } from "@/components/dashboard/quiet-hours";
import { RulesManager } from "@/components/dashboard/rules-manager";
import type { ChildProfile } from "@/lib/dashboard-model";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

type DeviceRow = {
  id: string;
  device_name: string | null;
  browser_name: string | null;
  status: string;
  last_seen_at: string | null;
  policy_fetched_at: string | null;
  paused_until: number | null;
};

// Device health from last_seen_at (any authenticated call) + policy_fetched_at
// (last policy-snapshot pull). Healthy means the extension is alive AND running
// current rules; "stale rules" means it talks to the API but hasn't refreshed
// its policy (broken/blocked extension); "offline" means silent for a day.
function deviceHealth(device: DeviceRow): { label: string; variant: "success" | "warning" | "secondary" | "destructive" } {
  const HOUR = 60 * 60 * 1000;
  const seenAt = device.last_seen_at ? new Date(device.last_seen_at).getTime() : 0;
  const policyAt = device.policy_fetched_at ? new Date(device.policy_fetched_at).getTime() : 0;
  const now = Date.now();
  if (now - seenAt > 24 * HOUR) return { label: "offline", variant: "destructive" };
  if (now - policyAt > HOUR) return { label: "stale rules", variant: "warning" };
  return { label: "protected", variant: "success" };
}

type Props = {
  children: ChildProfile[];
  getToken: () => Promise<string | null>;
  onCreatePairingCode: (childId: string) => Promise<string | null>;
  onStartMonitoring: (childId: string) => Promise<void>;
  onStopMonitoring: (childId: string) => Promise<void>;
};

export function ChildrenView(props: Props) {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Children</h1>
      {props.children.length === 0 ? (
        <p className="text-muted-foreground">No children yet. Add one from the dashboard.</p>
      ) : (
        props.children.map((child) => (
          <ChildCard key={child.id} child={child} {...props} />
        ))
      )}
    </div>
  );
}

function displayChildName(child: ChildProfile): string {
  if (child.name && child.name.trim()) return child.name;
  // Fall back to a readable name from the id (e.g. "child_sky_labs" -> "Sky Labs").
  const slug = child.id.replace(/^child_/, "").replace(/[_-]+/g, " ").trim();
  if (!slug) return child.id;
  return slug.replace(/\b\w/g, (c) => c.toUpperCase());
}

function ChildCard({ child, getToken, onCreatePairingCode, onStartMonitoring, onStopMonitoring }: { child: ChildProfile } & Props) {
  const [code, setCode] = useState<string | null>(null);
  const [devices, setDevices] = useState<DeviceRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showQuiet, setShowQuiet] = useState(false);
  const [showRules, setShowRules] = useState(false);
  const [showDevices, setShowDevices] = useState(false);
  const [monitoring, setMonitoring] = useState<boolean>(Boolean(child.monitoring_active));

  // A device is "active" only while monitoring is running; stopping drops the count to 0.
  // Prefer the live device list once loaded; fall back to the count from the children payload.
  const pairedActive = devices
    ? devices.filter((d) => d.status === "active").length
    : (child.active_device_count ?? 0);
  const activeDevices = monitoring ? pairedActive : 0;

  const loadDevices = async () => {
    setError(null);
    try {
      const authToken = await getToken();
      const data = await apiFetch<{ devices: DeviceRow[] }>(`/v1/children/${encodeURIComponent(child.id)}/devices`, authToken);
      setDevices(data.devices);
    } catch (e) {
      setError(String(e));
    }
  };

  const run = async (fn: () => Promise<void>, nextMonitoring: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setMonitoring(nextMonitoring);
      if (devices) await loadDevices();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-3">
          {displayChildName(child)}
          <Badge variant="secondary">age {child.age}</Badge>
          <Badge>{child.strictness}</Badge>
          <span className="text-sm font-normal text-muted-foreground">
            Monitoring is{" "}
            <strong className={monitoring ? "text-success" : "text-foreground"}>
              {monitoring ? "on" : "off"}
            </strong>
          </span>
          <span className="ml-auto text-sm font-medium text-success">
            Active Devices: {activeDevices}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap gap-2">
          <Button
            disabled={busy}
            variant={monitoring ? "outline" : "default"}
            onClick={() => run(() => onStartMonitoring(child.id), true)}
          >
            Start
          </Button>
          <Button
            disabled={busy}
            variant={monitoring ? "destructive" : "outline"}
            onClick={() => run(() => onStopMonitoring(child.id), false)}
          >
            Stop
          </Button>
          <Button
            disabled={busy}
            variant="outline"
            onClick={async () => {
              setBusy(true);
              try { setCode(await onCreatePairingCode(child.id)); } catch (e) { setError(String(e)); } finally { setBusy(false); }
            }}
          >
            Pair a device
          </Button>
          <Button
            disabled={busy}
            variant="ghost"
            onClick={() => {
              setShowDevices((v) => {
                const next = !v;
                if (next && !devices) void loadDevices();
                return next;
              });
            }}
          >
            {showDevices ? "Hide devices" : "View devices"}
          </Button>
          <Button variant="ghost" onClick={() => setShowQuiet((v) => !v)}>
            {showQuiet ? "Hide quiet hours" : "Quiet hours"}
          </Button>
          <Button variant="ghost" onClick={() => setShowRules((v) => !v)}>
            {showRules ? "Hide rules" : "Rules"}
          </Button>
        </div>

        {showQuiet && <QuietHours childId={child.id} getToken={getToken} />}
        {showRules && <RulesManager childId={child.id} getToken={getToken} />}

        {code && (
          <Alert>
            <AlertTitle>Pairing code</AlertTitle>
            <AlertDescription>Enter <strong>{code}</strong> in the WatchIt extension. It expires soon.</AlertDescription>
          </Alert>
        )}
        {error && <p className="text-sm text-destructive">{error}</p>}

        {showDevices && devices && (
          <div className="space-y-2">
            {devices.length === 0 && <p className="text-sm text-muted-foreground">No paired devices.</p>}
            {devices.map((d) => (
              <DeviceRowView key={d.id} device={d} monitoring={monitoring} getToken={getToken} onChanged={loadDevices} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function DeviceRowView({ device, monitoring, getToken, onChanged }: { device: DeviceRow; monitoring: boolean; getToken: () => Promise<string | null>; onChanged: () => Promise<void> }) {
  const [minutes, setMinutes] = useState("30");
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pausedActive = device.paused_until != null && device.paused_until > Date.now();
  const paired = device.status === "active";
  // Distinguish pairing (paired) from live monitoring. A paired device is only
  // "monitoring" while the child's session is running and it isn't paused.
  const monitoringNow = monitoring && paired && !pausedActive;

  const patch = async (paused_until_minutes: number, withPin: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const authToken = await getToken();
      await apiFetch(`/v1/devices/${encodeURIComponent(device.id)}`, authToken, {
        method: "PATCH",
        body: JSON.stringify(withPin ? { paused_until_minutes, pin } : { paused_until_minutes }),
      });
      setPin("");
      await onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm("Remove this device? It will stop working and disappear from the list.")) return;
    setBusy(true);
    setError(null);
    try {
      const authToken = await getToken();
      await apiFetch(`/v1/devices/${encodeURIComponent(device.id)}`, authToken, { method: "DELETE" });
      await onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-md border p-3 space-y-2">
      <div className="flex items-center gap-2">
        <span className="font-medium">{device.device_name || device.browser_name || device.id}</span>
        <Badge variant="outline">{paired ? "paired" : device.status}</Badge>
        {monitoringNow ? (
          <Badge variant="success">monitoring</Badge>
        ) : pausedActive ? (
          <Badge variant="secondary">paused</Badge>
        ) : (
          <Badge variant="secondary">not monitored</Badge>
        )}
        {paired && !pausedActive && (() => {
          const health = deviceHealth(device);
          return <Badge variant={health.variant}>{health.label}</Badge>;
        })()}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Input className="w-20" type="number" min={1} value={minutes} onChange={(e) => setMinutes(e.target.value)} aria-label="Pause minutes" />
        <Input className="w-28" type="password" placeholder="PIN" value={pin} onChange={(e) => setPin(e.target.value)} aria-label="Parent PIN" />
        <Button disabled={busy} onClick={() => patch(Math.max(1, parseInt(minutes || "0", 10)), true)}>Pause</Button>
        <Button disabled={busy} variant="secondary" onClick={() => patch(0, false)}>Resume</Button>
        <Button disabled={busy} variant="ghost" className="ml-auto text-destructive hover:text-destructive" onClick={remove}>Remove</Button>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  );
}
