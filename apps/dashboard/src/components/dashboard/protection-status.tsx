"use client";

import { useEffect, useState } from "react";
import { ShieldCheck, ShieldAlert } from "lucide-react";

import { apiFetch } from "@/lib/api-client";
import { useDashboardData } from "@/lib/dashboard-data";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";

type HouseholdDevice = {
  id: string;
  child_id: string;
  child_name: string | null;
  device_name: string | null;
  browser_name: string | null;
  status: string;
  last_seen_at: string | null;
  policy_fetched_at: string | null;
  paused_until: number | null;
};

type DeviceState = "protected" | "paused" | "stale" | "offline";

function deviceState(device: HouseholdDevice): DeviceState {
  const HOUR = 60 * 60 * 1000;
  if (device.paused_until != null && device.paused_until > Date.now()) return "paused";
  const seenAt = device.last_seen_at ? new Date(device.last_seen_at).getTime() : 0;
  const policyAt = device.policy_fetched_at ? new Date(device.policy_fetched_at).getTime() : 0;
  if (Date.now() - seenAt > 24 * HOUR) return "offline";
  if (Date.now() - policyAt > HOUR) return "stale";
  return "protected";
}

const STATE_LABEL: Record<DeviceState, { label: string; variant: "success" | "secondary" | "warning" | "destructive" }> = {
  protected: { label: "protected", variant: "success" },
  paused: { label: "paused", variant: "secondary" },
  stale: { label: "stale rules", variant: "warning" },
  offline: { label: "offline", variant: "destructive" },
};

// The one question the home view must answer first: is protection active right
// now, on every device, and if not — where.
export function ProtectionStatus() {
  const { token, security, selectedHousehold } = useDashboardData();
  const [devices, setDevices] = useState<HouseholdDevice[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const authToken = await token();
        const data = await apiFetch<{ devices: HouseholdDevice[] }>("/v1/devices", authToken);
        if (!cancelled) setDevices(data.devices || []);
      } catch {
        if (!cancelled) setDevices([]);
      }
    })();
    return () => { cancelled = true; };
  }, [token, selectedHousehold]);

  if (devices === null) return null;

  const monitoringOff = security ? security.monitoring_enabled === false : false;
  const problems = devices.filter((d) => deviceState(d) !== "protected");
  const allGood = !monitoringOff && devices.length > 0 && problems.length === 0;

  return (
    <Card>
      <CardContent className="flex flex-wrap items-center gap-3 py-4">
        {allGood ? (
          <ShieldCheck className="size-6 text-success" />
        ) : (
          <ShieldAlert className="size-6 text-warning" />
        )}
        <div className="min-w-0 flex-1">
          <p className="font-medium">
            {monitoringOff
              ? "Household monitoring is switched off"
              : devices.length === 0
                ? "No devices paired yet"
                : allGood
                  ? `Protection active on all ${devices.length} device${devices.length === 1 ? "" : "s"}`
                  : `${problems.length} of ${devices.length} devices need attention`}
          </p>
          {!monitoringOff && problems.length > 0 && (
            <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
              {problems.slice(0, 4).map((device) => {
                const state = STATE_LABEL[deviceState(device)];
                return (
                  <span key={device.id} className="flex items-center gap-1.5">
                    <span className="truncate max-w-40">{device.child_name || device.child_id}: {device.browser_name || device.device_name || device.id}</span>
                    <Badge variant={state.variant}>{state.label}</Badge>
                  </span>
                );
              })}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
