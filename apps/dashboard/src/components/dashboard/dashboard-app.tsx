"use client";

import Link from "next/link";
import { SignInButton, SignOutButton, useAuth, useUser } from "@clerk/nextjs";
import {
  Activity,
  AlertCircle,
  Bell,
  CalendarDays,
  Check,
  ChevronRight,
  CircleGauge,
  Download,
  Eye,
  KeyRound,
  Lock,
  MonitorCheck,
  Plus,
  RefreshCw,
  Settings,
  Shield,
  UserRound,
  UsersRound,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { apiFetch, ApiError, decisionStreamUrl } from "@/lib/api-client";
import { clientLogger, downloadClientLogFile } from "@/lib/client-logger";
import {
  attentionItems,
  categoryBreakdown,
  ChildProfile,
  computeMetrics,
  DecisionRecord,
  EventRecord,
  filterByChildAndRange,
  makeChildId,
  normalizeDecision,
  SecuritySettings,
  TimeRange,
} from "@/lib/dashboard-model";
import type { DashboardView as DashboardRouteView } from "@/lib/dashboard-model";
import { displayAction, displayReason, domainFromUrl, formatCountdown, formatDateTime, formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

const ACTION_OPTIONS = ["allow", "warn", "blur", "block", "notify"];
const RANGE_OPTIONS: { value: TimeRange; label: string }[] = [
  { value: "today", label: "Today" },
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
];

type DashboardAppProps = {
  initialView?: DashboardRouteView;
};

type ChildFormState = {
  name: string;
  age: number;
  strictness: string;
};

type PinFormState = {
  currentPin: string;
  newPin: string;
  confirmPin: string;
};

type IconComponent = React.ComponentType<{ className?: string }>;

export function DashboardApp({ initialView = "dashboard" }: DashboardAppProps) {
  const { isLoaded, isSignedIn, getToken } = useAuth();
  const { user } = useUser();
  const [children, setChildren] = useState<ChildProfile[]>([]);
  const [selectedChild, setSelectedChild] = useState<string | null>(null);
  const [activeChildId, setActiveChildId] = useState<string | null>(null);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [decisions, setDecisions] = useState<DecisionRecord[]>([]);
  const [security, setSecurity] = useState<SecuritySettings | null>(null);
  const [timeRange, setTimeRange] = useState<TimeRange>("today");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState<Record<string, boolean>>({});
  const [pairingCode, setPairingCode] = useState<string | null>(null);
  const [monitoringSession, setMonitoringSession] = useState<any>(null);
  const [decisionSaving, setDecisionSaving] = useState<Record<string, boolean>>({});
  const [childDialogOpen, setChildDialogOpen] = useState(false);
  const [childDialogMode, setChildDialogMode] = useState<"create" | "edit">("create");
  const [pinDialogOpen, setPinDialogOpen] = useState(false);
  const [pinDialogMode, setPinDialogMode] = useState<"setup" | "change">("setup");
  const [pinForm, setPinForm] = useState<PinFormState>({ currentPin: "", newPin: "", confirmPin: "" });
  const [pinMessage, setPinMessage] = useState<string | null>(null);
  const [pinSaving, setPinSaving] = useState(false);
  const [pausePin, setPausePin] = useState("");
  const [pauseMinutes, setPauseMinutes] = useState("");
  const [isPausedManual, setIsPausedManual] = useState(false);
  const [pausedUntilMs, setPausedUntilMs] = useState<number | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const esRef = useRef<EventSource | null>(null);

  const token = useCallback(() => getToken(), [getToken]);

  const refreshAll = useCallback(async () => {
    if (!isSignedIn) return;
    setError(null);
    setLoading(true);
    try {
      const authToken = await token();
      const [childrenResp, decisionsResp, eventsResp, securityResp] = await Promise.all([
        apiFetch<{ children: ChildProfile[]; active_child_id: string | null }>("/v1/children", authToken),
        apiFetch<{ decisions: DecisionRecord[] }>("/v1/decisions?limit=200", authToken),
        apiFetch<{ events: EventRecord[] }>("/v1/events?limit=200", authToken),
        apiFetch<SecuritySettings>("/v1/settings/security", authToken),
      ]);
      const childList = childrenResp.children || [];
      setChildren(childList);
      setActiveChildId(childrenResp.active_child_id || null);
      setSelectedChild((previous) => {
        if (!childList.length) return null;
        if (childrenResp.active_child_id && childList.some((child) => child.id === childrenResp.active_child_id)) {
          return childrenResp.active_child_id;
        }
        if (previous && childList.some((child) => child.id === previous)) return previous;
        return childList[0].id;
      });
      setDecisions((decisionsResp.decisions || []).map(normalizeDecision));
      setEvents(eventsResp.events || []);
      setSecurity(securityResp);
      clientLogger.info("dashboard data loaded", {
        children: childList.length,
        decisions: decisionsResp.decisions?.length || 0,
        events: eventsResp.events?.length || 0,
        parent_pin_set: securityResp.parent_pin_set,
      });
    } catch (err) {
      setError("Could not load dashboard data. Check the API service and try again.");
      clientLogger.error("failed to load dashboard data", { error: String(err) });
    } finally {
      setLoading(false);
    }
  }, [isSignedIn, token]);

  useEffect(() => {
    clientLogger.info("dashboard loaded");
    const stored = localStorage.getItem("paused_until");
    if (stored) {
      const val = parseInt(stored, 10);
      if (!Number.isNaN(val)) {
        setPausedUntilMs(val);
        setIsPausedManual(val > Date.now());
      }
    }
  }, []);

  useEffect(() => {
    if (!isSignedIn) {
      setChildren([]);
      setEvents([]);
      setDecisions([]);
      setSelectedChild(null);
      setSecurity(null);
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
      return;
    }
    refreshAll();
  }, [isSignedIn, refreshAll]);

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
              return [msg, ...previous].slice(0, 200);
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

  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (pausedUntilMs && pausedUntilMs <= nowMs) {
      setIsPausedManual(false);
      setPausedUntilMs(null);
      localStorage.removeItem("paused_until");
    }
  }, [pausedUntilMs, nowMs]);

  const selectedChildProfile = children.find((child) => child.id === selectedChild) || null;
  const filteredDecisions = useMemo(
    () => filterByChildAndRange(decisions, selectedChild, timeRange),
    [decisions, selectedChild, timeRange],
  );
  const filteredEvents = useMemo(
    () => filterByChildAndRange(events, selectedChild, timeRange),
    [events, selectedChild, timeRange],
  );
  const metrics = useMemo(() => computeMetrics(filteredDecisions, filteredEvents), [filteredDecisions, filteredEvents]);
  const attention = useMemo(
    () => attentionItems(filteredDecisions).filter((item) => !dismissed[item.id]),
    [dismissed, filteredDecisions],
  );
  const categories = useMemo(
    () => categoryBreakdown(filteredDecisions, filteredEvents),
    [filteredDecisions, filteredEvents],
  );
  const isPaused = isPausedManual || Boolean(pausedUntilMs && pausedUntilMs > nowMs);
  const needsOnboarding = Boolean(security && (!security.parent_pin_set || children.length === 0));

  const handleCreateChild = async (form: ChildFormState) => {
    const childId = makeChildId(form.name);
    if (!childId) throw new Error("Enter a child name.");
    const authToken = await token();
    await apiFetch(`/v1/children/${encodeURIComponent(childId)}/settings`, authToken, {
      method: "POST",
      body: JSON.stringify({ name: form.name.trim(), strictness: form.strictness, age: form.age }),
    });
    setChildDialogOpen(false);
    await refreshAll();
    setSelectedChild(childId);
  };

  const handleUpdateChild = async (form: ChildFormState) => {
    if (!selectedChild) return;
    const authToken = await token();
    await apiFetch(`/v1/children/${encodeURIComponent(selectedChild)}/settings`, authToken, {
      method: "POST",
      body: JSON.stringify({ name: form.name.trim(), strictness: form.strictness, age: form.age }),
    });
    setChildDialogOpen(false);
    await refreshAll();
  };

  const submitPin = async () => {
    setPinMessage(null);
    const pinPolicy = security?.pin_policy || { min_length: 4, max_length: 8, digits_only: true };
    const valid = new RegExp(`^\\d{${pinPolicy.min_length},${pinPolicy.max_length}}$`).test(pinForm.newPin);
    if (!valid) {
      setPinMessage(`PIN must be ${pinPolicy.min_length}-${pinPolicy.max_length} digits.`);
      return;
    }
    if (pinForm.newPin !== pinForm.confirmPin) {
      setPinMessage("PIN confirmation does not match.");
      return;
    }
    setPinSaving(true);
    try {
      const authToken = await token();
      await apiFetch<{ ok: boolean; parent_pin_set: boolean }>("/v1/settings/parent-pin", authToken, {
        method: "POST",
        body: JSON.stringify({
          current_pin: security?.parent_pin_set ? pinForm.currentPin : undefined,
          new_pin: pinForm.newPin,
        }),
      });
      setPinForm({ currentPin: "", newPin: "", confirmPin: "" });
      setPinDialogOpen(false);
      setSecurity((previous) => previous ? { ...previous, parent_pin_set: true } : previous);
      await refreshAll();
    } catch (err) {
      const apiError = err as ApiError;
      setPinMessage(apiError.detail === "current_pin_required" ? "Enter your current PIN first." : apiError.message);
    } finally {
      setPinSaving(false);
    }
  };

  const pauseMonitoring = async () => {
    try {
      const mins = parseInt(pauseMinutes || "0", 10);
      const authToken = await token();
      const data = await apiFetch<{ ok: boolean; paused_until: number }>("/v1/control/pause", authToken, {
        method: "POST",
        body: JSON.stringify({ pin: pausePin, minutes: Number.isFinite(mins) ? mins : undefined }),
      });
      setIsPausedManual(true);
      setPausedUntilMs(data.paused_until);
      localStorage.setItem("paused_until", String(data.paused_until));
      setPausePin("");
      setPauseMinutes("");
    } catch (err) {
      const apiError = err as ApiError;
      if (apiError.status === 409 && apiError.detail === "parent_pin_required") {
        setPinDialogMode("setup");
        setPinDialogOpen(true);
        return;
      }
      setError(apiError.message || "Could not pause monitoring.");
    }
  };

  const resumeMonitoring = async () => {
    const authToken = await token();
    await apiFetch("/v1/control/resume", authToken, { method: "POST", body: JSON.stringify({}) });
    setIsPausedManual(false);
    setPausedUntilMs(null);
    localStorage.removeItem("paused_until");
  };

  const createPairingCode = async () => {
    if (!selectedChild) return;
    const authToken = await token();
    const data = await apiFetch<{ pairing_code: { code: string } }>("/v1/device/pairing-codes", authToken, {
      method: "POST",
      body: JSON.stringify({ child_id: selectedChild, ttl_minutes: 15 }),
    });
    setPairingCode(data.pairing_code?.code || null);
  };

  const startMonitoring = async () => {
    if (!selectedChild) return;
    const authToken = await token();
    const data = await apiFetch<{ session: any }>("/v1/monitoring/start", authToken, {
      method: "POST",
      body: JSON.stringify({ child_id: selectedChild }),
    });
    setMonitoringSession(data.session);
  };

  const stopMonitoring = async () => {
    const authToken = await token();
    await apiFetch("/v1/monitoring/stop", authToken, {
      method: "POST",
      body: JSON.stringify({ child_id: selectedChild }),
    });
    setMonitoringSession(null);
  };

  const overrideDecision = async (decisionId: string, action: string) => {
    setDecisionSaving((previous) => ({ ...previous, [decisionId]: true }));
    try {
      const authToken = await token();
      const data = await apiFetch<{ decision: DecisionRecord }>(`/v1/decisions/${decisionId}/override`, authToken, {
        method: "POST",
        body: JSON.stringify({ action }),
      });
      const normalized = normalizeDecision(data.decision);
      setDecisions((previous) => previous.map((decision) => decision.id === normalized.id ? normalized : decision));
    } finally {
      setDecisionSaving((previous) => {
        const next = { ...previous };
        delete next[decisionId];
        return next;
      });
    }
  };

  if (!isLoaded) return <DashboardSkeleton />;
  if (!isSignedIn) return <SignedOut />;

  return (
    <div className="min-h-screen bg-background text-foreground">
      <AppShell activeView={initialView} userName={user?.fullName || user?.primaryEmailAddress?.emailAddress || "Guardian"}>
        <div className="space-y-6">
          <Header
            userName={user?.firstName || user?.fullName || "Guardian"}
            children={children}
            selectedChild={selectedChild}
            setSelectedChild={setSelectedChild}
            timeRange={timeRange}
            setTimeRange={setTimeRange}
            onAddChild={() => {
              setChildDialogMode("create");
              setChildDialogOpen(true);
            }}
          />

          {error && (
            <Alert variant="destructive">
              <AlertCircle className="size-4" />
              <AlertTitle>Dashboard needs attention</AlertTitle>
              <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
                <span>{error}</span>
                <Button variant="outline" size="sm" onClick={refreshAll}>
                  <RefreshCw className="size-4" /> Retry
                </Button>
              </AlertDescription>
            </Alert>
          )}

          {loading ? (
            <DashboardSkeleton />
          ) : initialView === "settings" ? (
            <SettingsView
              security={security}
              isPaused={isPaused}
              pausedUntilMs={pausedUntilMs}
              nowMs={nowMs}
              pausePin={pausePin}
              setPausePin={setPausePin}
              pauseMinutes={pauseMinutes}
              setPauseMinutes={setPauseMinutes}
              onPause={pauseMonitoring}
              onResume={resumeMonitoring}
              onOpenPin={() => {
                setPinDialogMode(security?.parent_pin_set ? "change" : "setup");
                setPinDialogOpen(true);
              }}
            />
          ) : initialView === "profile" ? (
            <ProfileView user={user} />
          ) : (
            <HomeDashboardView
              needsOnboarding={needsOnboarding}
              parentPinSet={Boolean(security?.parent_pin_set)}
              hasChildren={children.length > 0}
              selectedChild={selectedChildProfile}
              activeChildId={activeChildId}
              metrics={metrics}
              attention={attention}
              decisions={filteredDecisions}
              events={filteredEvents}
              categories={categories}
              isPaused={isPaused}
              pausedUntilMs={pausedUntilMs}
              nowMs={nowMs}
              pairingCode={pairingCode}
              monitoringSession={monitoringSession}
              decisionSaving={decisionSaving}
              onSetupPin={() => {
                setPinDialogMode(security?.parent_pin_set ? "change" : "setup");
                setPinDialogOpen(true);
              }}
              onAddChild={() => {
                setChildDialogMode("create");
                setChildDialogOpen(true);
              }}
              onPair={createPairingCode}
              onStartMonitoring={startMonitoring}
              onStopMonitoring={stopMonitoring}
              onDismiss={(id) => setDismissed((previous) => ({ ...previous, [id]: true }))}
              onOverride={overrideDecision}
              onEditChild={() => {
                setChildDialogMode("edit");
                setChildDialogOpen(true);
              }}
              onResume={resumeMonitoring}
            />
          )}
        </div>
      </AppShell>

      <ChildProfileDialog
        open={childDialogOpen}
        onOpenChange={setChildDialogOpen}
        child={childDialogMode === "edit" ? selectedChildProfile : null}
        onCreate={handleCreateChild}
        onUpdate={handleUpdateChild}
      />
      <PinDialog
        open={pinDialogOpen}
        onOpenChange={setPinDialogOpen}
        mode={pinDialogMode}
        pinSet={Boolean(security?.parent_pin_set)}
        form={pinForm}
        setForm={setPinForm}
        message={pinMessage}
        saving={pinSaving}
        onSubmit={submitPin}
      />
    </div>
  );
}

function SignedOut() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-6">
      <Card className="max-w-md">
        <CardHeader>
          <div className="mb-3 flex size-11 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Shield className="size-5" />
          </div>
          <CardTitle className="text-2xl">WatchIt Guardian Dashboard</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          <p className="text-sm text-muted-foreground">
            Sign in to review browsing activity, set up child profiles, pair the browser extension, and manage protection settings.
          </p>
          <SignInButton mode="modal">
            <Button className="w-full">Sign in</Button>
          </SignInButton>
        </CardContent>
      </Card>
    </main>
  );
}

function AppShell({ activeView, userName, children }: { activeView: DashboardRouteView; userName: string; children: React.ReactNode }) {
  const nav = [
    { view: "dashboard", href: "/", label: "Dashboard", icon: CircleGauge },
    { view: "settings", href: "/settings", label: "Settings", icon: Settings },
    { view: "profile", href: "/profile", label: "Profile", icon: UserRound },
  ] as const;
  return (
    <div className="mx-auto grid min-h-screen w-full max-w-7xl grid-cols-1 gap-0 lg:grid-cols-[240px_1fr]">
      <aside className="border-b border-border bg-card/60 p-4 lg:border-b-0 lg:border-r">
        <div className="flex items-center gap-3">
          <div className="flex size-9 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Shield className="size-4" />
          </div>
          <div>
            <p className="text-sm font-semibold">WatchIt</p>
            <p className="text-xs text-muted-foreground">Parent console</p>
          </div>
        </div>
        <nav className="mt-6 flex gap-2 lg:flex-col">
          {nav.map((item) => {
            const Icon = item.icon;
            return (
              <Button key={item.view} asChild variant={activeView === item.view ? "secondary" : "ghost"} className="justify-start">
                <Link href={item.href}>
                  <Icon className="size-4" /> {item.label}
                </Link>
              </Button>
            );
          })}
        </nav>
      </aside>
      <main className="min-w-0 p-4 sm:p-6 lg:p-8">
        <div className="mb-6 flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <Avatar>
              <AvatarFallback>{userName.slice(0, 2).toUpperCase()}</AvatarFallback>
            </Avatar>
            <div>
              <p className="text-sm font-medium">{userName}</p>
              <p className="text-xs text-muted-foreground">Signed in guardian</p>
            </div>
          </div>
          <SignOutButton>
            <Button variant="outline" size="sm">Sign out</Button>
          </SignOutButton>
        </div>
        {children}
      </main>
    </div>
  );
}

function Header(props: {
  userName: string;
  children: ChildProfile[];
  selectedChild: string | null;
  setSelectedChild: (value: string) => void;
  timeRange: TimeRange;
  setTimeRange: (value: TimeRange) => void;
  onAddChild: () => void;
}) {
  return (
    <section className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
      <div>
        <p className="text-sm text-muted-foreground">Welcome back, {props.userName}</p>
        <h1 className="mt-1 text-3xl font-semibold tracking-tight">Family safety overview</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
          See what happened online, what needs attention, and whether protection is active.
        </p>
      </div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <Select value={props.selectedChild || ""} onValueChange={props.setSelectedChild}>
          <SelectTrigger className="w-full sm:w-[190px]">
            <SelectValue placeholder="Select child" />
          </SelectTrigger>
          <SelectContent>
            {props.children.map((child) => (
              <SelectItem key={child.id} value={child.id}>{child.name || child.id}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <div className="flex rounded-lg border bg-card p-1">
          {RANGE_OPTIONS.map((option) => (
            <Button
              key={option.value}
              variant={props.timeRange === option.value ? "secondary" : "ghost"}
              size="sm"
              onClick={() => props.setTimeRange(option.value)}
            >
              {option.label}
            </Button>
          ))}
        </div>
        <Button onClick={props.onAddChild}>
          <Plus className="size-4" /> Add child
        </Button>
      </div>
    </section>
  );
}

function HomeDashboardView(props: {
  needsOnboarding: boolean;
  parentPinSet: boolean;
  hasChildren: boolean;
  selectedChild: ChildProfile | null;
  activeChildId: string | null;
  metrics: ReturnType<typeof computeMetrics>;
  attention: ReturnType<typeof attentionItems>;
  decisions: DecisionRecord[];
  events: EventRecord[];
  categories: ReturnType<typeof categoryBreakdown>;
  isPaused: boolean;
  pausedUntilMs: number | null;
  nowMs: number;
  pairingCode: string | null;
  monitoringSession: any;
  decisionSaving: Record<string, boolean>;
  onSetupPin: () => void;
  onAddChild: () => void;
  onPair: () => void;
  onStartMonitoring: () => void;
  onStopMonitoring: () => void;
  onDismiss: (id: string) => void;
  onOverride: (id: string, action: string) => void;
  onEditChild: () => void;
  onResume: () => void;
}) {
  if (props.needsOnboarding) {
    return (
      <OnboardingPanel
        parentPinSet={props.parentPinSet}
        hasChildren={props.hasChildren}
        selectedChild={props.selectedChild}
        monitoringSession={props.monitoringSession}
        pairingCode={props.pairingCode}
        onSetupPin={props.onSetupPin}
        onAddChild={props.onAddChild}
        onPair={props.onPair}
        onStartMonitoring={props.onStartMonitoring}
      />
    );
  }
  return (
    <div className="space-y-6">
      <SafetyOverview metrics={props.metrics} />
      <div className="grid gap-6 xl:grid-cols-[1.35fr_.85fr]">
        <AttentionNeeded
          items={props.attention}
          saving={props.decisionSaving}
          onDismiss={props.onDismiss}
          onOverride={props.onOverride}
        />
        <ControlsSummary {...props} />
      </div>
      <div className="grid gap-6 xl:grid-cols-[1.2fr_.8fr]">
        <ActivityTimeline decisions={props.decisions} events={props.events} />
        <CategoryBreakdown items={props.categories} />
      </div>
    </div>
  );
}

function OnboardingPanel(props: {
  parentPinSet: boolean;
  hasChildren: boolean;
  selectedChild: ChildProfile | null;
  monitoringSession: any;
  pairingCode: string | null;
  onSetupPin: () => void;
  onAddChild: () => void;
  onPair: () => void;
  onStartMonitoring: () => void;
}) {
  const steps = [
    { label: "Set parent PIN", complete: props.parentPinSet, action: props.onSetupPin },
    { label: "Create child profile", complete: props.hasChildren, action: props.onAddChild },
    { label: "Pair browser extension", complete: Boolean(props.pairingCode), action: props.onPair, disabled: !props.selectedChild },
    { label: "Start monitoring", complete: Boolean(props.monitoringSession), action: props.onStartMonitoring, disabled: !props.selectedChild },
  ];
  return (
    <Card>
      <CardHeader>
        <Badge variant="secondary" className="w-fit">First-time setup</Badge>
        <CardTitle className="text-2xl">Protect your child’s browser in a few steps</CardTitle>
        <p className="text-sm text-muted-foreground">
          WatchIt starts showing activity after a child profile is connected to the browser extension and monitoring is active.
        </p>
      </CardHeader>
      <CardContent className="grid gap-4 md:grid-cols-2">
        {steps.map((step) => (
          <div key={step.label} className="flex items-center justify-between rounded-lg border p-4">
            <div className="flex items-center gap-3">
              <div className={cn("flex size-8 items-center justify-center rounded-full", step.complete ? "bg-emerald-500/15 text-emerald-400" : "bg-muted text-muted-foreground")}>
                {step.complete ? <Check className="size-4" /> : <ChevronRight className="size-4" />}
              </div>
              <span className="font-medium">{step.label}</span>
            </div>
            <Button size="sm" variant={step.complete ? "outline" : "default"} disabled={step.disabled} onClick={step.action}>
              {step.complete ? "Done" : "Start"}
            </Button>
          </div>
        ))}
        {props.pairingCode && (
          <Alert className="md:col-span-2">
            <KeyRound className="size-4" />
            <AlertTitle>Extension pairing code</AlertTitle>
            <AlertDescription>Enter <strong>{props.pairingCode}</strong> in the WatchIt browser extension. It expires soon.</AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}

function SafetyOverview({ metrics }: { metrics: ReturnType<typeof computeMetrics> }) {
  const cards = [
    { label: "Safety score", value: metrics.safetyScore === null ? "--" : `${metrics.safetyScore}`, helper: "Overall browsing health", icon: Shield },
    { label: "Sites checked", value: metrics.sitesChecked, helper: "Unique sites in range", icon: Eye },
    { label: "Flagged pages", value: metrics.flaggedCount, helper: "Warned, hidden, blocked, or notified", icon: Bell },
    { label: "Parent changes", value: metrics.overrideCount, helper: "Manual corrections", icon: UsersRound },
  ];
  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
      {cards.map((card) => {
        const Icon = card.icon;
        return (
          <Card key={card.label}>
            <CardContent className="p-5">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <p className="text-sm text-muted-foreground">{card.label}</p>
                  <p className="mt-2 text-3xl font-semibold">{card.value}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{card.helper}</p>
                </div>
                <Icon className="size-5 text-muted-foreground" />
              </div>
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}

function AttentionNeeded(props: {
  items: ReturnType<typeof attentionItems>;
  saving: Record<string, boolean>;
  onDismiss: (id: string) => void;
  onOverride: (id: string, action: string) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Attention needed</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {props.items.length === 0 ? (
          <EmptyMini icon={Check} title="Nothing urgent right now" text="Risky or blocked pages will appear here for quick review." />
        ) : props.items.map((item) => (
          <div key={item.id} className="rounded-lg border p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <Badge className={riskClass(item.riskLevel)}>{item.riskLevel} risk</Badge>
                <h3 className="mt-2 truncate font-medium">{item.title || domainFromUrl(item.url)}</h3>
                <p className="mt-1 text-sm text-muted-foreground">{displayReason(item.reason)}</p>
                <p className="mt-1 text-xs text-muted-foreground">{formatDateTime(item.ts)} · {domainFromUrl(item.url)}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" disabled={!item.url} onClick={() => item.url && window.open(item.url, "_blank", "noreferrer")}>Review</Button>
                <Button size="sm" variant="outline" disabled={props.saving[item.id]} onClick={() => props.onOverride(item.id, "allow")}>Allow</Button>
                <Button size="sm" variant="destructive" disabled={props.saving[item.id]} onClick={() => props.onOverride(item.id, "block")}>Block</Button>
                <Button size="sm" variant="ghost" onClick={() => props.onDismiss(item.id)}>Dismiss</Button>
              </div>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function ControlsSummary(props: {
  parentPinSet: boolean;
  selectedChild: ChildProfile | null;
  activeChildId: string | null;
  isPaused: boolean;
  pausedUntilMs: number | null;
  nowMs: number;
  pairingCode: string | null;
  monitoringSession: any;
  onSetupPin: () => void;
  onPair: () => void;
  onStartMonitoring: () => void;
  onStopMonitoring: () => void;
  onEditChild: () => void;
  onResume: () => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Controls and setup</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <StatusLine icon={KeyRound} label="Parent PIN" value={props.parentPinSet ? "Set" : "Needs setup"} good={props.parentPinSet} />
        <StatusLine
          icon={MonitorCheck}
          label="Monitoring"
          value={props.isPaused ? `Paused ${props.pausedUntilMs ? `· ${formatCountdown(props.pausedUntilMs - props.nowMs)} left` : ""}` : props.monitoringSession ? "Live" : "Not started"}
          good={!props.isPaused && Boolean(props.monitoringSession)}
        />
        <StatusLine icon={Activity} label="Active child" value={props.selectedChild?.name || props.selectedChild?.id || "None"} good={Boolean(props.selectedChild)} />
        <Separator />
        <div className="grid gap-2 sm:grid-cols-2">
          <Button variant="outline" onClick={props.onSetupPin}>{props.parentPinSet ? "Change PIN" : "Set PIN"}</Button>
          <Button variant="outline" onClick={props.onEditChild} disabled={!props.selectedChild}>Edit child</Button>
          <Button variant="outline" onClick={props.onPair} disabled={!props.selectedChild}>Pair extension</Button>
          <Button onClick={props.monitoringSession ? props.onStopMonitoring : props.onStartMonitoring} disabled={!props.selectedChild}>
            {props.monitoringSession ? "Stop monitoring" : "Start monitoring"}
          </Button>
        </div>
        {props.isPaused && <Button variant="secondary" className="w-full" onClick={props.onResume}>Resume monitoring</Button>}
        {props.pairingCode && (
          <Alert>
            <KeyRound className="size-4" />
            <AlertTitle>Pairing code ready</AlertTitle>
            <AlertDescription>Enter <strong>{props.pairingCode}</strong> in the extension.</AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}

function ActivityTimeline({ decisions, events }: { decisions: DecisionRecord[]; events: EventRecord[] }) {
  const rows = [
    ...decisions.map((decision) => ({ type: "decision", ts: Number(decision.ts || 0), item: decision })),
    ...events.map((event) => ({ type: "event", ts: Number(event.ts || 0), item: event })),
  ].sort((a, b) => b.ts - a.ts).slice(0, 12);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Child activity timeline</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {rows.length === 0 ? (
          <EmptyMini icon={CalendarDays} title="No activity yet" text="Activity appears here after the paired extension starts monitoring." />
        ) : rows.map((row, index) => {
          const decision = row.type === "decision" ? row.item as DecisionRecord : null;
          const event = row.item as EventRecord;
          return (
            <div key={`${row.type}-${event.id || decision?.id || index}`} className="flex gap-3">
              <div className="mt-1 size-2 rounded-full bg-primary" />
              <div className="min-w-0 flex-1 border-b pb-3 last:border-b-0">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={decision?.action === "allow" || !decision ? "secondary" : "default"}>
                    {decision ? displayAction(decision.action) : "Visit"}
                  </Badge>
                  {decision?.manual_flagged && <Badge variant="outline">Parent changed</Badge>}
                  <span className="text-xs text-muted-foreground">{formatTime(event.ts || decision?.ts)}</span>
                </div>
                <p className="mt-1 truncate text-sm font-medium">{event.title || decision?.title || domainFromUrl(event.url || decision?.url)}</p>
                <p className="truncate text-xs text-muted-foreground">{domainFromUrl(event.url || decision?.url)}</p>
              </div>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

function CategoryBreakdown({ items }: { items: ReturnType<typeof categoryBreakdown> }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Category breakdown</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {items.map((item) => (
          <div key={item.label} className="space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span>{item.label}</span>
              <span className="text-muted-foreground">{item.count}</span>
            </div>
            <Progress value={item.percent} />
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function SettingsView(props: {
  security: SecuritySettings | null;
  isPaused: boolean;
  pausedUntilMs: number | null;
  nowMs: number;
  pausePin: string;
  setPausePin: (value: string) => void;
  pauseMinutes: string;
  setPauseMinutes: (value: string) => void;
  onPause: () => void;
  onResume: () => void;
  onOpenPin: () => void;
}) {
  return (
    <Tabs defaultValue="security" className="space-y-6">
      <TabsList>
        <TabsTrigger value="security">Security</TabsTrigger>
        <TabsTrigger value="controls">Controls</TabsTrigger>
        <TabsTrigger value="logs">Logs</TabsTrigger>
      </TabsList>
      <TabsContent value="security">
        <Card>
          <CardHeader>
            <CardTitle>Parent PIN</CardTitle>
            <p className="text-sm text-muted-foreground">This PIN protects pause controls and local parent overrides.</p>
          </CardHeader>
          <CardContent className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <StatusLine icon={Lock} label="PIN status" value={props.security?.parent_pin_set ? "Set" : "Not set"} good={Boolean(props.security?.parent_pin_set)} />
            <Button onClick={props.onOpenPin}>{props.security?.parent_pin_set ? "Change PIN" : "Set PIN"}</Button>
          </CardContent>
        </Card>
      </TabsContent>
      <TabsContent value="controls">
        <Card>
          <CardHeader>
            <CardTitle>Pause monitoring</CardTitle>
            <p className="text-sm text-muted-foreground">Use this when a parent temporarily wants monitoring paused.</p>
          </CardHeader>
          <CardContent className="grid gap-3 sm:grid-cols-[1fr_1fr_auto]">
            <Input type="password" inputMode="numeric" placeholder="Parent PIN" value={props.pausePin} onChange={(event) => props.setPausePin(event.target.value)} />
            <Input type="number" min={1} placeholder="Minutes, blank for indefinite" value={props.pauseMinutes} onChange={(event) => props.setPauseMinutes(event.target.value)} />
            {props.isPaused ? <Button variant="secondary" onClick={props.onResume}>Resume</Button> : <Button onClick={props.onPause}>Pause</Button>}
            {props.isPaused && (
              <p className="text-sm text-muted-foreground sm:col-span-3">
                Monitoring is paused {props.pausedUntilMs ? `for ${formatCountdown(props.pausedUntilMs - props.nowMs)}` : "indefinitely"}.
              </p>
            )}
          </CardContent>
        </Card>
      </TabsContent>
      <TabsContent value="logs">
        <Card>
          <CardHeader>
            <CardTitle>Client logs</CardTitle>
            <p className="text-sm text-muted-foreground">Export recent browser-side dashboard logs for debugging.</p>
          </CardHeader>
          <CardContent>
            <Button variant="outline" onClick={downloadClientLogFile}><Download className="size-4" /> Export logs</Button>
          </CardContent>
        </Card>
      </TabsContent>
    </Tabs>
  );
}

function ProfileView({ user }: { user: { fullName?: string | null; primaryEmailAddress?: { emailAddress?: string | null } | null } | null | undefined }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Parent profile</CardTitle>
        <p className="text-sm text-muted-foreground">Your WatchIt dashboard uses your Clerk account for identity.</p>
      </CardHeader>
      <CardContent className="space-y-4">
        <StatusLine icon={UserRound} label="Name" value={user?.fullName || "Not set"} good />
        <StatusLine icon={Bell} label="Email" value={user?.primaryEmailAddress?.emailAddress || "Not set"} good={Boolean(user?.primaryEmailAddress)} />
        <Separator />
        <SignOutButton>
          <Button variant="outline">Sign out</Button>
        </SignOutButton>
      </CardContent>
    </Card>
  );
}

function ChildProfileDialog(props: {
  open: boolean;
  onOpenChange: (value: boolean) => void;
  child: ChildProfile | null;
  onCreate: (form: ChildFormState) => Promise<void>;
  onUpdate: (form: ChildFormState) => Promise<void>;
}) {
  const [form, setForm] = useState<ChildFormState>({ name: "", age: 12, strictness: "standard" });
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (props.open) {
      setForm({
        name: props.child?.name || props.child?.id?.replace(/^child_/, "") || "",
        age: props.child?.age || 12,
        strictness: props.child?.strictness || "standard",
      });
      setMessage(null);
    }
  }, [props.child, props.open]);

  const submit = async () => {
    if (!form.name.trim()) {
      setMessage("Enter a child name.");
      return;
    }
    setSaving(true);
    try {
      if (props.child) await props.onUpdate(form);
      else await props.onCreate(form);
    } catch (err) {
      setMessage(String((err as Error).message || err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={props.open} onOpenChange={props.onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{props.child ? "Edit child profile" : "Add child profile"}</DialogTitle>
          <DialogDescription>Use a name parents recognize. Protection level can be changed later.</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="child-name">Child name</Label>
            <Input id="child-name" value={form.name} onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))} />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="child-age">Age</Label>
              <Input id="child-age" type="number" min={3} max={18} value={form.age} onChange={(event) => setForm((prev) => ({ ...prev, age: clampAge(event.target.value) }))} />
            </div>
            <div className="space-y-2">
              <Label>Protection level</Label>
              <Select value={form.strictness} onValueChange={(value) => setForm((prev) => ({ ...prev, strictness: value }))}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="lenient">Lenient</SelectItem>
                  <SelectItem value="standard">Standard</SelectItem>
                  <SelectItem value="strict">Strict</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          {message && <p className="text-sm text-destructive">{message}</p>}
          <Button className="w-full" onClick={submit} disabled={saving}>{saving ? "Saving..." : "Save child profile"}</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function PinDialog(props: {
  open: boolean;
  onOpenChange: (value: boolean) => void;
  mode: "setup" | "change";
  pinSet: boolean;
  form: PinFormState;
  setForm: React.Dispatch<React.SetStateAction<PinFormState>>;
  message: string | null;
  saving: boolean;
  onSubmit: () => void;
}) {
  const showCurrent = props.pinSet && props.mode === "change";
  return (
    <Dialog open={props.open} onOpenChange={props.onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{showCurrent ? "Change parent PIN" : "Set parent PIN"}</DialogTitle>
          <DialogDescription>This PIN protects pause controls and local parent overrides. Use 4-8 digits.</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          {showCurrent && (
            <div className="space-y-2">
              <Label htmlFor="current-pin">Current PIN</Label>
              <Input id="current-pin" type="password" inputMode="numeric" value={props.form.currentPin} onChange={(event) => props.setForm((prev) => ({ ...prev, currentPin: event.target.value }))} />
            </div>
          )}
          <div className="space-y-2">
            <Label htmlFor="new-pin">New PIN</Label>
            <Input id="new-pin" type="password" inputMode="numeric" value={props.form.newPin} onChange={(event) => props.setForm((prev) => ({ ...prev, newPin: event.target.value }))} />
          </div>
          <div className="space-y-2">
            <Label htmlFor="confirm-pin">Confirm PIN</Label>
            <Input id="confirm-pin" type="password" inputMode="numeric" value={props.form.confirmPin} onChange={(event) => props.setForm((prev) => ({ ...prev, confirmPin: event.target.value }))} />
          </div>
          {props.message && <p className="text-sm text-destructive">{props.message}</p>}
          <Button className="w-full" onClick={props.onSubmit} disabled={props.saving}>{props.saving ? "Saving..." : "Save PIN"}</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function StatusLine({ icon: Icon, label, value, good }: { icon: IconComponent; label: string; value: string; good: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border p-3">
      <div className="flex items-center gap-3">
        <Icon className="size-4 text-muted-foreground" />
        <span className="text-sm text-muted-foreground">{label}</span>
      </div>
      <Badge variant={good ? "secondary" : "outline"}>{value}</Badge>
    </div>
  );
}

function EmptyMini({ icon: Icon, title, text }: { icon: IconComponent; title: string; text: string }) {
  return (
    <div className="rounded-lg border border-dashed p-6 text-center">
      <Icon className="mx-auto size-6 text-muted-foreground" />
      <p className="mt-3 font-medium">{title}</p>
      <p className="mt-1 text-sm text-muted-foreground">{text}</p>
    </div>
  );
}

function DashboardSkeleton() {
  return (
    <div className="space-y-6">
      <div className="grid gap-4 md:grid-cols-4">
        {[0, 1, 2, 3].map((item) => <Skeleton key={item} className="h-32" />)}
      </div>
      <div className="grid gap-6 xl:grid-cols-2">
        <Skeleton className="h-80" />
        <Skeleton className="h-80" />
      </div>
    </div>
  );
}

function riskClass(level: string) {
  if (level === "high") return "bg-red-500/15 text-red-300 hover:bg-red-500/20";
  if (level === "medium") return "bg-amber-500/15 text-amber-300 hover:bg-amber-500/20";
  return "bg-blue-500/15 text-blue-300 hover:bg-blue-500/20";
}

function clampAge(value: string) {
  return Math.min(18, Math.max(3, parseInt(value || "0", 10) || 3));
}
