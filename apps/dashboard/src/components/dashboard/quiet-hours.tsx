"use client";

import { useCallback, useEffect, useState } from "react";
import { Clock, Trash2 } from "lucide-react";

import { apiFetch } from "@/lib/api-client";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const ALL_DEVICES = "__all__";

type Schedule = {
  id: string;
  device_id: string | null;
  name?: string;
  days: string;
  quiet_start: string;
  quiet_end: string;
  enabled: boolean;
};

type DeviceLite = { id: string; device_name: string | null; browser_name: string | null };

type FormState = {
  scheduleId: string | null;
  deviceId: string; // ALL_DEVICES or a device id
  days: string[];
  start: string;
  end: string;
  enabled: boolean;
};

const EMPTY_FORM: FormState = {
  scheduleId: null,
  deviceId: ALL_DEVICES,
  days: ["Mon", "Tue", "Wed", "Thu"],
  start: "21:00",
  end: "07:00",
  enabled: true,
};

export function QuietHours({ childId, getToken }: { childId: string; getToken: () => Promise<string | null> }) {
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [devices, setDevices] = useState<DeviceLite[]>([]);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const authToken = await getToken();
      const [sched, devs] = await Promise.all([
        apiFetch<{ schedules: Schedule[] }>(`/v1/children/${encodeURIComponent(childId)}/schedules`, authToken),
        apiFetch<{ devices: DeviceLite[] }>(`/v1/children/${encodeURIComponent(childId)}/devices`, authToken),
      ]);
      setSchedules(sched.schedules || []);
      setDevices(devs.devices || []);
    } catch (e) {
      setError(String(e));
    }
  }, [childId, getToken]);

  useEffect(() => {
    void load();
  }, [load]);

  const deviceLabel = (id: string | null) => {
    if (!id) return "All devices";
    const d = devices.find((x) => x.id === id);
    return d?.device_name || d?.browser_name || id;
  };

  const save = async () => {
    if (form.days.length === 0) {
      setError("Pick at least one day.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const authToken = await getToken();
      await apiFetch(`/v1/children/${encodeURIComponent(childId)}/schedules`, authToken, {
        method: "POST",
        body: JSON.stringify({
          schedule_id: form.scheduleId,
          device_id: form.deviceId === ALL_DEVICES ? null : form.deviceId,
          days: form.days.join(","),
          quiet_start: form.start,
          quiet_end: form.end,
          enabled: form.enabled,
        }),
      });
      setForm(EMPTY_FORM);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    setBusy(true);
    setError(null);
    try {
      const authToken = await getToken();
      await apiFetch(`/v1/schedules/${encodeURIComponent(id)}`, authToken, { method: "DELETE" });
      if (form.scheduleId === id) setForm(EMPTY_FORM);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const editSchedule = (s: Schedule) => {
    setForm({
      scheduleId: s.id,
      deviceId: s.device_id || ALL_DEVICES,
      days: s.days.split(",").map((d) => d.trim()).filter(Boolean),
      start: s.quiet_start,
      end: s.quiet_end,
      enabled: s.enabled,
    });
  };

  const toggleDay = (day: string) =>
    setForm((prev) => ({
      ...prev,
      days: prev.days.includes(day) ? prev.days.filter((d) => d !== day) : [...prev.days, day],
    }));

  return (
    <div className="space-y-4 rounded-lg border bg-muted/30 p-4">
      <div className="flex items-center gap-2">
        <Clock className="size-4 text-muted-foreground" />
        <span className="font-medium">Quiet hours</span>
        <span className="text-xs text-muted-foreground">Everything except this dashboard is blocked during these windows.</span>
      </div>

      {schedules.length > 0 && (
        <div className="space-y-2">
          {schedules.map((s) => (
            <div key={s.id} className="flex flex-col gap-2 rounded-md border bg-background p-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <Badge variant="outline">{deviceLabel(s.device_id)}</Badge>
                <Badge variant={s.enabled ? "success" : "secondary"}>{s.enabled ? "on" : "off"}</Badge>
                <span className="font-medium">{s.quiet_start}–{s.quiet_end}</span>
                <span className="text-muted-foreground">{s.days}</span>
              </div>
              <div className="flex gap-2">
                <Button size="sm" variant="outline" disabled={busy} onClick={() => editSchedule(s)}>Edit</Button>
                <Button size="sm" variant="ghost" className="text-destructive hover:text-destructive" disabled={busy} onClick={() => remove(s.id)}>
                  <Trash2 className="size-4" />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="space-y-3 rounded-md border bg-background p-3">
        <p className="text-sm font-medium">{form.scheduleId ? "Edit window" : "Add window"}</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>Applies to</Label>
            <Select value={form.deviceId} onValueChange={(value) => setForm((prev) => ({ ...prev, deviceId: value }))}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL_DEVICES}>All devices (child default)</SelectItem>
                {devices.map((d) => (
                  <SelectItem key={d.id} value={d.id}>{d.device_name || d.browser_name || d.id}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-end gap-2">
            <div className="space-y-1.5">
              <Label htmlFor={`qs-${childId}`}>Start</Label>
              <Input id={`qs-${childId}`} type="time" value={form.start} onChange={(e) => setForm((prev) => ({ ...prev, start: e.target.value }))} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={`qe-${childId}`}>End</Label>
              <Input id={`qe-${childId}`} type="time" value={form.end} onChange={(e) => setForm((prev) => ({ ...prev, end: e.target.value }))} />
            </div>
          </div>
        </div>

        <div className="space-y-1.5">
          <Label>Days</Label>
          <div className="flex flex-wrap gap-1.5">
            {DAYS.map((day) => {
              const active = form.days.includes(day);
              return (
                <button
                  key={day}
                  type="button"
                  onClick={() => toggleDay(day)}
                  className={cn(
                    "rounded-md border px-2.5 py-1 text-xs font-medium transition-colors",
                    active
                      ? "border-primary bg-primary text-primary-foreground"
                      : "border-border bg-background text-muted-foreground hover:bg-muted",
                  )}
                >
                  {day}
                </button>
              );
            })}
          </div>
        </div>

        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Switch checked={form.enabled} onCheckedChange={(v) => setForm((prev) => ({ ...prev, enabled: v }))} aria-label="Enabled" />
            <span className="text-sm text-muted-foreground">Enabled</span>
          </div>
          <div className="flex gap-2">
            {form.scheduleId && (
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => setForm(EMPTY_FORM)}>Cancel</Button>
            )}
            <Button size="sm" disabled={busy} onClick={save}>{form.scheduleId ? "Update" : "Add window"}</Button>
          </div>
        </div>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  );
}
