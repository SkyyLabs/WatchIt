"use client";

import { useState } from "react";
import { apiFetch } from "@/lib/api-client";
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
  paused_until: number | null;
};

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

function ChildCard({ child, getToken, onCreatePairingCode, onStartMonitoring, onStopMonitoring }: { child: ChildProfile } & Props) {
  const [code, setCode] = useState<string | null>(null);
  const [devices, setDevices] = useState<DeviceRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
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
          {child.name}
          <Badge variant="secondary">age {child.age}</Badge>
          <Badge>{child.strictness}</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap gap-2">
          <Button disabled={busy} onClick={() => run(() => onStartMonitoring(child.id))}>Play</Button>
          <Button disabled={busy} variant="secondary" onClick={() => run(() => onStopMonitoring(child.id))}>Stop</Button>
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
          <Button disabled={busy} variant="ghost" onClick={loadDevices}>View devices</Button>
        </div>

        {code && (
          <Alert>
            <AlertTitle>Pairing code</AlertTitle>
            <AlertDescription>Enter <strong>{code}</strong> in the WatchIt extension. It expires soon.</AlertDescription>
          </Alert>
        )}
        {error && <p className="text-sm text-destructive">{error}</p>}

        {devices && (
          <div className="space-y-2">
            {devices.length === 0 && <p className="text-sm text-muted-foreground">No paired devices.</p>}
            {devices.map((d) => (
              <DeviceRowView key={d.id} device={d} getToken={getToken} onChanged={loadDevices} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function DeviceRowView({ device, getToken, onChanged }: { device: DeviceRow; getToken: () => Promise<string | null>; onChanged: () => Promise<void> }) {
  const [minutes, setMinutes] = useState("30");
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pausedActive = device.paused_until != null && device.paused_until > Date.now();

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

  return (
    <div className="rounded-md border p-3 space-y-2">
      <div className="flex items-center gap-2">
        <span className="font-medium">{device.device_name || device.browser_name || device.id}</span>
        <Badge variant={device.status === "active" ? "default" : "secondary"}>{device.status}</Badge>
        {pausedActive && <Badge variant="outline">paused</Badge>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Input className="w-20" type="number" min={1} value={minutes} onChange={(e) => setMinutes(e.target.value)} aria-label="Pause minutes" />
        <Input className="w-28" type="password" placeholder="PIN" value={pin} onChange={(e) => setPin(e.target.value)} aria-label="Parent PIN" />
        <Button disabled={busy} onClick={() => patch(Math.max(1, parseInt(minutes || "0", 10)), true)}>Pause</Button>
        <Button disabled={busy} variant="secondary" onClick={() => patch(0, false)}>Resume</Button>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  );
}
