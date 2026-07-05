# Children & Monitoring Control Page — Design

Date: 2026-07-04
Status: Approved (pending spec review)

## Goal

A guardian dashboard page at `/children` that acts as the household's monitoring
control center. For each child it shows profile (name, age, strictness), lets the
guardian pair a new device (on-demand code), lists that child's paired devices, and
exposes monitoring controls: Play/Stop (monitoring on/off) and per-device timed pause.

## Decisions locked during brainstorming

- **Pairing display:** on-demand short-lived code per child (fits the existing
  one-time/hashed pairing model). No persistent per-device "pin" — that concept does
  not exist and would break the one-time + hashed-code invariants.
- **Monitoring granularity:** on/off stays **per-child** (one active session per child;
  `device_id` is metadata). Play/Stop affect all of a child's devices together.
- **Timed pause:** **per-device**, new capability. State lives on the `devices` table
  (not the session, which is per-child), so it can be scoped to a single device.
- **Parent PIN:** required for Stop and for Pause (matches existing `control/pause`).
  Play (start) does not require it.
- **HTTP verbs:** use PATCH where the request is genuinely a partial update of a
  resource; keep POST for creates/append/actions.

## HTTP surface

### New / changed to PATCH

- `PATCH /v1/devices/{device_id}` (`require_guardian`, household-scoped) — device pause state.
  - Pause: `{ "paused_until_minutes": 30, "pin": "1234" }` → sets `devices.paused_until = now + 30m`. PIN required.
  - Resume: `{ "paused_until_minutes": 0 }` → clears `paused_until`. No PIN required.
  - 404 if the device is not in the resolved household. 403 on bad PIN. 409 if PIN not set.
- `PATCH /v1/children/{child_id}` (`require_guardian`, household-scoped) — updates child
  profile (name, age, strictness). Replaces `POST /v1/children/{child_id}/settings`.
- `PATCH /v1/control` (`require_guardian`) — household global pause/resume. Replaces
  `POST /v1/control/pause` + `POST /v1/control/resume`.
  - Pause: `{ "paused_until_minutes": N, "pin": "1234" }` (N>0). PIN required.
  - Resume: `{ "paused_until_minutes": 0 }`.

### New read

- `GET /v1/children/{child_id}/devices` (`require_guardian`, household-scoped) →
  `{ "devices": [...] }`. 404 if child not in household.

### Unchanged (stay POST — reasons)

- `POST /v1/decisions/{id}/override` — overrides are **new rows**, never mutations
  (auditability invariant). PATCH would wrongly imply in-place edit.
- `POST /v1/event`, `/v1/event/upgrade`, `/v1/device/pairing-codes`, `/v1/device/redeem` —
  creates / actions.
- `POST /v1/monitoring/start|stop` — Play/Stop; `start` inserts an audit session row.
- `POST /v1/settings/parent-pin` — sensitive auth action (needs current PIN).

## Data layer (`packages/core`)

- **Migration** (`migrations/versions/20260704_NNNN_devices_paused_until.py`): add
  `devices.paused_until` — nullable `BIGINT` (epoch ms), consistent with the existing
  household `paused_until` setting which is stored as ms. Single Alembic head preserved.
- `db.fetch_devices(household_id, child_id)` — new repository method.
  `SELECT id, child_id, device_name, browser_name, status, last_seen_at, paused_until, created_at
  FROM devices WHERE household_id=%s AND child_id=%s ORDER BY created_at DESC`.
  **Never selects `token_hash`.**
- `db.set_device_pause(household_id, device_id, paused_until_ms, guardian_id)` and
  `db.clear_device_pause(household_id, device_id, guardian_id)` — household-scoped writes.
- `db.get_device_paused_until(device_id)` — for the runtime gate. Returns int ms or None.
- `db.update_child_profile` already exists — reuse for `PATCH /v1/children/{id}`.

## Runtime gate (`services/agent-worker/runtime.py`)

Extend the existing pause check. Today it is:

```
paused_until = db.get_paused_until(household_id)
if paused_until and now_ms < paused_until: <bypass pipeline, decision = allow>
```

Change to also honor a per-device pause:

```
household_paused = db.get_paused_until(household_id)
device_paused = db.get_device_paused_until(event.device_id) if event.device_id else None
effective = max(filter(None, [household_paused, device_paused]), default=None)
if effective and now_ms < effective: <bypass, decision = allow, reason = "paused">
```

Behavior while paused mirrors the current global pause exactly: the event is still
recorded, decision is `allow`, and it auto-resumes lazily when the clock passes
`paused_until` — no scheduler. One extra indexed read by `device_id` on the pause path.

## Frontend (`apps/dashboard`)

- `app/children/page.tsx` → `<DashboardApp initialView="children" />` (mirrors profile/settings).
- `lib/dashboard-model.ts`: add `"children"` to `DashboardView`.
- `dashboard-app.tsx`: add a nav item `{ view: "children", href: "/children", label: "Children", icon: Users }` (lucide `Users`)
  and route `initialView === "children"` to the new view.
- `components/dashboard/children-view.tsx` (new file — `dashboard-app.tsx` is already ~1168 lines):
  - One card per child: name, age, strictness badge; **Pair a device** button (calls the
    existing `POST /v1/device/pairing-codes` with that `child_id`, shows the code inline once).
  - **Play / Stop** buttons per child (existing `POST /v1/monitoring/start|stop`). Stop prompts for PIN.
  - **View devices**: fetches `GET /v1/children/{id}/devices`, lists each device (name,
    status badge, relative last-seen, paused-until state). Per device: **Pause for…**
    (minutes input + PIN → `PATCH /v1/devices/{id}`) and **Resume** (`PATCH /v1/devices/{id}` with 0).
- PIN entry: reuse existing PIN input pattern used by the current pause control.
- Update the existing edit-child client call to `PATCH /v1/children/{id}`.
- Update the existing dashboard pause/resume client calls to `PATCH /v1/control`.

## Security

- Every read/write scoped to the resolved household (`require_guardian`); child and
  device ownership verified server-side; never trust client-submitted ids.
- PIN required for Stop, device Pause, and household Pause; verified server-side.
- Pairing codes stay short-lived and one-time; shown once in the UI; never logged.
- `token_hash` never leaves the API. No secrets in client bundle or logs.

## Testing

- `db.fetch_devices` scoping: does not return another household's or another child's devices.
- `GET /v1/children/{id}/devices`: 404 on cross-household child.
- `PATCH /v1/devices/{id}`: sets/clears `paused_until`; 403 on bad PIN; 409 when PIN unset;
  404 cross-household.
- Runtime gate: a device with future `paused_until` bypasses the pipeline as `allow` and
  auto-resumes once the timestamp passes; household pause and device pause compose (max).
- `PATCH /v1/children/{id}` and `PATCH /v1/control` behave as the replaced POST routes did.
- Dashboard typecheck via the build check.

## Out of scope (YAGNI)

- Device revoke/rename from this page.
- True per-device monitoring on/off (kept per-child).
- Any scheduler/cron for auto-resume (lazy check is sufficient).
- Converting other POST endpoints to PATCH beyond the three listed.
