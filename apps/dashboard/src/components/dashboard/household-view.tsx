"use client";

import { useCallback, useEffect, useState } from "react";
import { Home, Plus, ArrowRightLeft } from "lucide-react";

import { apiFetch } from "@/lib/api-client";
import { useDashboardData } from "@/lib/dashboard-data";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

type GuardianChild = {
  id: string;
  household_id: string;
  household_name?: string;
  name?: string;
  strictness?: string;
  age?: number;
  status?: string;
};

export function HouseholdView() {
  const { households, selectedHousehold, setSelectedHousehold, token, refreshCore, security, setSecurity } = useDashboardData();
  const [guardianChildren, setGuardianChildren] = useState<GuardianChild[]>([]);
  const [createOpen, setCreateOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [monitoringBusy, setMonitoringBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const monitoringEnabled = security?.monitoring_enabled ?? true;

  const toggleMonitoring = async (enabled: boolean) => {
    setMonitoringBusy(true);
    setError(null);
    try {
      const authToken = await token();
      await apiFetch("/v1/control/monitoring", authToken, {
        method: "POST",
        body: JSON.stringify({ enabled }),
      });
      setSecurity((prev) => (prev ? { ...prev, monitoring_enabled: enabled } : prev));
    } catch (e) {
      setError(String(e));
    } finally {
      setMonitoringBusy(false);
    }
  };

  const loadChildren = useCallback(async () => {
    setError(null);
    try {
      const authToken = await token();
      const data = await apiFetch<{ children: GuardianChild[] }>("/v1/guardian/children", authToken);
      setGuardianChildren(data.children || []);
    } catch (e) {
      setError(String(e));
    }
  }, [token]);

  useEffect(() => {
    void loadChildren();
  }, [loadChildren, selectedHousehold]);

  const currentHousehold = households.find((h) => h.id === selectedHousehold) || null;
  const here = guardianChildren.filter((c) => c.household_id === selectedHousehold);
  const elsewhere = guardianChildren.filter((c) => c.household_id !== selectedHousehold);
  const otherHouseholds = households.filter((h) => h.id !== selectedHousehold);

  const moveChild = async (childId: string, targetHouseholdId: string) => {
    setBusy(true);
    setError(null);
    try {
      const authToken = await token();
      await apiFetch(`/v1/children/${encodeURIComponent(childId)}/move`, authToken, {
        method: "POST",
        body: JSON.stringify({ target_household_id: targetHouseholdId }),
      });
      await Promise.all([loadChildren(), refreshCore()]);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const createHousehold = async (name: string) => {
    setBusy(true);
    setError(null);
    try {
      const authToken = await token();
      const data = await apiFetch<{ household: { id: string } }>("/v1/households", authToken, {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      await refreshCore();
      if (data.household?.id) setSelectedHousehold(data.household.id);
      setCreateOpen(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Households</h1>
          <p className="mt-1 text-sm text-muted-foreground">Create households and organize which children belong where.</p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="size-4" /> New household
        </Button>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}

      <Card>
        <CardContent className="flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <p className="font-medium">Household monitoring</p>
              <Badge variant={monitoringEnabled ? "success" : "secondary"}>{monitoringEnabled ? "On" : "Off"}</Badge>
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              Master switch for {currentHousehold?.name || "this household"}. Off pauses all analysis; children browse unmonitored.
            </p>
          </div>
          <Switch
            checked={monitoringEnabled}
            disabled={monitoringBusy}
            onCheckedChange={toggleMonitoring}
            aria-label="Toggle household monitoring"
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Home className="size-4 text-muted-foreground" />
            {currentHousehold?.name || "Current household"}
            {currentHousehold?.role && <Badge variant="secondary">{currentHousehold.role}</Badge>}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {here.length === 0 ? (
            <p className="text-sm text-muted-foreground">No children in this household yet.</p>
          ) : (
            here.map((child) => (
              <div key={child.id} className="flex flex-col gap-3 rounded-lg border p-4 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{child.name || child.id}</span>
                  {child.age != null && <Badge variant="secondary">age {child.age}</Badge>}
                  {child.strictness && <Badge>{child.strictness}</Badge>}
                </div>
                {otherHouseholds.length === 0 ? (
                  <span className="text-xs text-muted-foreground">Create another household to move this child.</span>
                ) : (
                  <MoveControl
                    label="Remove to"
                    households={otherHouseholds}
                    disabled={busy}
                    onMove={(target) => moveChild(child.id, target)}
                  />
                )}
              </div>
            ))
          )}
        </CardContent>
      </Card>

      {elsewhere.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Add an existing child</CardTitle>
            <p className="text-sm text-muted-foreground">Children in your other households — move one into {currentHousehold?.name || "this household"}.</p>
          </CardHeader>
          <CardContent className="space-y-3">
            {elsewhere.map((child) => (
              <div key={child.id} className="flex flex-col gap-3 rounded-lg border p-4 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{child.name || child.id}</span>
                  <Badge variant="outline">{child.household_name || child.household_id}</Badge>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy || !selectedHousehold}
                  onClick={() => selectedHousehold && moveChild(child.id, selectedHousehold)}
                >
                  <ArrowRightLeft className="size-4" /> Add here
                </Button>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <CreateHouseholdDialog open={createOpen} onOpenChange={setCreateOpen} busy={busy} onCreate={createHousehold} />
    </div>
  );
}

function MoveControl({
  label,
  households,
  disabled,
  onMove,
}: {
  label: string;
  households: { id: string; name: string }[];
  disabled: boolean;
  onMove: (targetHouseholdId: string) => void;
}) {
  const [target, setTarget] = useState<string>("");
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-muted-foreground">{label}</span>
      <Select value={target} onValueChange={setTarget}>
        <SelectTrigger className="w-[160px]">
          <SelectValue placeholder="Household" />
        </SelectTrigger>
        <SelectContent>
          {households.map((h) => (
            <SelectItem key={h.id} value={h.id}>{h.name}</SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Button size="sm" variant="outline" disabled={disabled || !target} onClick={() => target && onMove(target)}>
        Move
      </Button>
    </div>
  );
}

function CreateHouseholdDialog({
  open,
  onOpenChange,
  busy,
  onCreate,
}: {
  open: boolean;
  onOpenChange: (value: boolean) => void;
  busy: boolean;
  onCreate: (name: string) => void;
}) {
  const [name, setName] = useState("");
  useEffect(() => {
    if (open) setName("");
  }, [open]);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New household</DialogTitle>
          <DialogDescription>Group children and devices under a household you manage.</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="household-name">Household name</Label>
            <Input id="household-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Home" />
          </div>
          <Button className="w-full" disabled={busy || !name.trim()} onClick={() => onCreate(name.trim())}>
            {busy ? "Creating..." : "Create household"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
