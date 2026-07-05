# Children & Monitoring Control Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/children` guardian page that shows each child's profile, pairs devices, lists devices, and controls monitoring (per-child Play/Stop, per-device timed pause).

**Architecture:** New per-device pause state on the `devices` table, honored by the existing runtime pause gate. New device-list read endpoint. Three POST→PATCH conversions for genuine resource updates. New Next.js route + a dedicated `children-view.tsx` component reusing existing UI primitives and the existing pairing/monitoring endpoints.

**Tech Stack:** FastAPI + Pydantic, psycopg (Postgres/Neon), Alembic, structlog; Next.js App Router + Clerk + Tailwind + shadcn/ui; pytest.

## Global Constraints

- Every Python command needs `PYTHONPATH="apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src"` (the Makefile exports it). Prefer `make verify` for the full check.
- Alembic-only schema, **single head** (`alembic heads` shows one). New revision chains from `20260622_0003`. No `watchit_` table prefix.
- `db.py` is the single repository layer; repository methods name their scope (`household_id`, `child_id`, `device_id`). Never select or return `token_hash`.
- Two auth models, never mixed: guardian routes → `require_guardian`. Scope every query by the resolved household; never trust client-submitted ids.
- Parent PIN stored hashed only; verify server-side via `db.verify_parent_pin`. Pairing codes short-lived/one-time; never logged.
- Pause timestamps are epoch **milliseconds** (`BIGINT`), matching the existing household `paused_until` setting.
- Dashboard never touches Postgres — HTTP API via `apiFetch` only. Client components start with `"use client"`.
- Payload Pydantic models live inline in `apps/api/src/watchit_api/main.py` (existing convention), not `schemas.py`.

---

### Task 1: Migration — add `devices.paused_until`

**Files:**
- Create: `migrations/versions/20260704_0004_devices_paused_until.py`

**Interfaces:**
- Produces: column `devices.paused_until BIGINT NULL` (epoch ms).

- [ ] **Step 1: Write the migration**

```python
"""per-device timed pause

Revision ID: 20260704_0004
Revises: 20260622_0003
Create Date: 2026-07-04
"""

from alembic import op
import sqlalchemy as sa

revision = "20260704_0004"
down_revision = "20260622_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("paused_until", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "paused_until")
```

- [ ] **Step 2: Verify single head**

Run: `PYTHONPATH="apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src" .venv/bin/alembic heads`
Expected: exactly one head, `20260704_0004 (head)`.

- [ ] **Step 3: Commit**

```bash
git add migrations/versions/20260704_0004_devices_paused_until.py
git commit -m "feat: add devices.paused_until column for per-device pause"
```

Note: Do not run `alembic upgrade` against Neon casually. The API/worker run migrations on startup in local dev; the column applies then.

---

### Task 2: Repository methods for devices + device pause

**Files:**
- Modify: `packages/core/src/watchit_core/db.py`
- Test: `tests/test_device_repository.py`

**Interfaces:**
- Produces:
  - `db.fetch_devices(household_id: str, child_id: str) -> list[dict]` — rows with keys `id, child_id, device_name, browser_name, status, last_seen_at, paused_until, created_at` (no `token_hash`).
  - `db.get_device_paused_until(device_id: str) -> int | None`
  - `db.set_device_pause(household_id: str, device_id: str, paused_until_ms: int) -> int` (returns affected row count)
  - `db.clear_device_pause(household_id: str, device_id: str) -> int`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_device_repository.py
import inspect
from watchit_core.db import Database


def test_fetch_devices_never_selects_token_hash():
    src = inspect.getsource(Database.fetch_devices)
    assert "token_hash" not in src
    # scoped by household and child
    assert "household_id" in src and "child_id" in src


def test_device_pause_methods_are_household_scoped():
    for name in ("set_device_pause", "clear_device_pause"):
        src = inspect.getsource(getattr(Database, name))
        assert "household_id" in src, f"{name} must scope by household_id"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src" .venv/bin/pytest tests/test_device_repository.py -v`
Expected: FAIL — `AttributeError: type object 'Database' has no attribute 'fetch_devices'`.

- [ ] **Step 3: Add the methods**

Add to `packages/core/src/watchit_core/db.py` (near `fetch_children`, ~line 206):

```python
    def fetch_devices(self, household_id: str, child_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, child_id, device_name, browser_name, status,
                       last_seen_at, paused_until, created_at
                FROM devices
                WHERE household_id=%s AND child_id=%s
                ORDER BY created_at DESC
                """,
                (household_id, child_id),
            )
            return cur.fetchall()

    def get_device_paused_until(self, device_id: str) -> Optional[int]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT paused_until FROM devices WHERE id=%s", (device_id,))
            row = cur.fetchone()
            return row["paused_until"] if row else None

    def set_device_pause(self, household_id: str, device_id: str, paused_until_ms: int) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devices SET paused_until=%s, updated_at=now() WHERE id=%s AND household_id=%s",
                (paused_until_ms, device_id, household_id),
            )
            return cur.rowcount

    def clear_device_pause(self, household_id: str, device_id: str) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devices SET paused_until=NULL, updated_at=now() WHERE id=%s AND household_id=%s",
                (device_id, household_id),
            )
            return cur.rowcount
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src" .venv/bin/pytest tests/test_device_repository.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add packages/core/src/watchit_core/db.py tests/test_device_repository.py
git commit -m "feat: add device list and per-device pause repository methods"
```

---

### Task 3: `GET /v1/children/{child_id}/devices`

**Files:**
- Modify: `apps/api/src/watchit_api/main.py` (add route near `list_children`, ~line 300)
- Test: `tests/test_children_devices_endpoint.py`

**Interfaces:**
- Consumes: `db.fetch_devices`, `db.get_child_profile`.
- Produces: `GET /v1/children/{child_id}/devices` → `{"devices": [...]}`; 404 when child not in household.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_children_devices_endpoint.py
import asyncio
import pytest
from fastapi import HTTPException
from watchit_api import main


def _guardian_ctx():
    return {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}


def test_devices_404_when_child_not_in_household(monkeypatch):
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid: None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.list_child_devices("child_x", guardian_ctx=_guardian_ctx()))
    assert exc.value.status_code == 404


def test_devices_returns_scoped_list(monkeypatch):
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid: {"id": cid})
    captured = {}
    def fake_fetch(household_id, child_id):
        captured["args"] = (household_id, child_id)
        return [{"id": "dev_1", "device_name": "Chrome"}]
    monkeypatch.setattr(main.db, "fetch_devices", fake_fetch)
    out = asyncio.run(main.list_child_devices("child_1", guardian_ctx=_guardian_ctx()))
    assert out == {"devices": [{"id": "dev_1", "device_name": "Chrome"}]}
    assert captured["args"] == ("hh_1", "child_1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src" .venv/bin/pytest tests/test_children_devices_endpoint.py -v`
Expected: FAIL — `AttributeError: module 'watchit_api.main' has no attribute 'list_child_devices'`.

- [ ] **Step 3: Add the route**

Insert after the `list_children` handler in `apps/api/src/watchit_api/main.py`:

```python
@app.get("/v1/children/{child_id}/devices")
async def list_child_devices(child_id: str, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    if not db.get_child_profile(child_id, household_id):
        raise HTTPException(404, "child not found")
    return {"devices": db.fetch_devices(household_id, child_id)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src" .venv/bin/pytest tests/test_children_devices_endpoint.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/watchit_api/main.py tests/test_children_devices_endpoint.py
git commit -m "feat: add GET /v1/children/{id}/devices"
```

---

### Task 4: `PATCH /v1/devices/{device_id}` — device pause/resume

**Files:**
- Modify: `apps/api/src/watchit_api/main.py` (payload class near line 152; route near device routes ~line 381)
- Test: `tests/test_device_pause_endpoint.py`

**Interfaces:**
- Consumes: `db.set_device_pause`, `db.clear_device_pause`, `db.fetch_devices`, `db.is_parent_pin_set`, `db.verify_parent_pin`.
- Produces: `PATCH /v1/devices/{device_id}` with body `DevicePatchPayload(paused_until_minutes: int, pin: str | None)`.
  - `paused_until_minutes > 0` → pause (PIN required); `== 0` → resume (no PIN).
  - Returns `{"ok": True, "paused_until": int | None}`. 404 unknown device, 403 bad PIN, 409 PIN unset.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_device_pause_endpoint.py
import asyncio
import time
import pytest
from fastapi import HTTPException
from watchit_api import main


def _ctx():
    return {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}


def _payload(minutes, pin=None):
    return main.DevicePatchPayload(paused_until_minutes=minutes, pin=pin)


def test_resume_clears_pause(monkeypatch):
    calls = {}
    monkeypatch.setattr(main.db, "clear_device_pause", lambda hid, did: calls.setdefault("cleared", (hid, did)) or 1)
    out = asyncio.run(main.patch_device("dev_1", _payload(0), guardian_ctx=_ctx()))
    assert out == {"ok": True, "paused_until": None}
    assert calls["cleared"] == ("hh_1", "dev_1")


def test_pause_requires_pin(monkeypatch):
    monkeypatch.setattr(main.db, "is_parent_pin_set", lambda hid: True)
    monkeypatch.setattr(main.db, "verify_parent_pin", lambda pin, hid: False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.patch_device("dev_1", _payload(30, "0000"), guardian_ctx=_ctx()))
    assert exc.value.status_code == 403


def test_pause_sets_future_timestamp(monkeypatch):
    monkeypatch.setattr(main.db, "is_parent_pin_set", lambda hid: True)
    monkeypatch.setattr(main.db, "verify_parent_pin", lambda pin, hid: True)
    saved = {}
    monkeypatch.setattr(main.db, "set_device_pause", lambda hid, did, until: saved.setdefault("v", until) or 1)
    out = asyncio.run(main.patch_device("dev_1", _payload(30, "1234"), guardian_ctx=_ctx()))
    assert out["ok"] is True
    assert out["paused_until"] > int(time.time() * 1000)
    assert saved["v"] == out["paused_until"]


def test_pause_unknown_device_404(monkeypatch):
    monkeypatch.setattr(main.db, "is_parent_pin_set", lambda hid: True)
    monkeypatch.setattr(main.db, "verify_parent_pin", lambda pin, hid: True)
    monkeypatch.setattr(main.db, "set_device_pause", lambda hid, did, until: 0)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.patch_device("nope", _payload(30, "1234"), guardian_ctx=_ctx()))
    assert exc.value.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src" .venv/bin/pytest tests/test_device_pause_endpoint.py -v`
Expected: FAIL — `AttributeError: module 'watchit_api.main' has no attribute 'DevicePatchPayload'`.

- [ ] **Step 3: Add payload + route**

Add the payload class near the other payloads (~line 152) in `apps/api/src/watchit_api/main.py`:

```python
class DevicePatchPayload(BaseModel):
    paused_until_minutes: int
    pin: Optional[str] = None
```

Ensure `Optional` is imported (`from typing import Optional`). Add the route near the device endpoints:

```python
@app.patch("/v1/devices/{device_id}")
async def patch_device(device_id: str, body: DevicePatchPayload, guardian_ctx=Depends(require_guardian)):
    import time
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    if body.paused_until_minutes <= 0:
        db.clear_device_pause(household_id, device_id)
        db.log_audit(household_id, "device_resumed", guardian_id=guardian_id, device_id=device_id, entity_type="device", entity_id=device_id)
        logger.info("device_resumed", device_id=device_id)
        return {"ok": True, "paused_until": None}
    if not db.is_parent_pin_set(household_id):
        raise HTTPException(409, "parent_pin_required")
    if not db.verify_parent_pin(body.pin or "", household_id):
        raise HTTPException(403, "Invalid PIN")
    until_ms = int(time.time() * 1000 + body.paused_until_minutes * 60 * 1000)
    if db.set_device_pause(household_id, device_id, until_ms) == 0:
        raise HTTPException(404, "device not found")
    db.log_audit(household_id, "device_paused", guardian_id=guardian_id, device_id=device_id, entity_type="device", entity_id=device_id, metadata={"minutes": body.paused_until_minutes})
    logger.info("device_paused", device_id=device_id, paused_until_ms=until_ms)
    return {"ok": True, "paused_until": until_ms}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src" .venv/bin/pytest tests/test_device_pause_endpoint.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/watchit_api/main.py tests/test_device_pause_endpoint.py
git commit -m "feat: add PATCH /v1/devices/{id} for per-device timed pause"
```

---

### Task 5: Runtime gate honors per-device pause

**Files:**
- Modify: `services/agent-worker/src/watchit_agents/runtime.py` (pause gate, ~lines 182-193)
- Test: `tests/test_runtime_device_pause.py`

**Interfaces:**
- Consumes: `db.get_paused_until(household_id)`, `db.get_device_paused_until(device_id)`.
- Produces: a helper `effective_pause_until(db, household_id, device_id) -> int | None` returning the later of household and device pause (max), or None. The gate bypasses the pipeline (decision `allow`) when `now_ms < effective`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runtime_device_pause.py
from watchit_agents.runtime import effective_pause_until


class FakeDB:
    def __init__(self, hh, dev):
        self._hh, self._dev = hh, dev
    def get_paused_until(self, household_id):
        return self._hh
    def get_device_paused_until(self, device_id):
        return self._dev


def test_returns_none_when_neither_paused():
    assert effective_pause_until(FakeDB(None, None), "hh_1", "dev_1") is None


def test_device_pause_applies_without_household_pause():
    assert effective_pause_until(FakeDB(None, 5000), "hh_1", "dev_1") == 5000


def test_takes_the_later_of_the_two():
    assert effective_pause_until(FakeDB(9000, 5000), "hh_1", "dev_1") == 9000


def test_missing_device_id_uses_household_only():
    assert effective_pause_until(FakeDB(7000, None), "hh_1", None) == 7000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src" .venv/bin/pytest tests/test_runtime_device_pause.py -v`
Expected: FAIL — `ImportError: cannot import name 'effective_pause_until'`.

- [ ] **Step 3: Add helper and use it in the gate**

Add near the top-level helpers in `services/agent-worker/src/watchit_agents/runtime.py`:

```python
def effective_pause_until(db, household_id, device_id):
    household = db.get_paused_until(household_id)
    device = db.get_device_paused_until(device_id) if device_id else None
    candidates = [c for c in (household, device) if c]
    return max(candidates) if candidates else None
```

Then update the pause gate (currently around line 185):

```python
    # Global + per-device pause gate: short-circuit the pipeline while paused.
    pause_started = time.perf_counter()
    paused_until = effective_pause_until(db, household_id, event.get("device_id"))
    pause_check_ms = _elapsed_ms(pause_started)
    if paused_until and now_ms < paused_until:
        # ...existing bypass body unchanged...
```

Leave the rest of the bypass body (logging, decision = allow, writes, publish) exactly as-is.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src" .venv/bin/pytest tests/test_runtime_device_pause.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add services/agent-worker/src/watchit_agents/runtime.py tests/test_runtime_device_pause.py
git commit -m "feat: runtime pause gate honors per-device paused_until"
```

---

### Task 6: `PATCH /v1/children/{child_id}` (replace `POST /v1/children/{id}/settings`)

**Files:**
- Modify: `apps/api/src/watchit_api/main.py` (change decorator + signature of the child-settings handler, ~line 307)
- Test: `tests/test_children_patch_endpoint.py`

**Interfaces:**
- Consumes: `db.update_child_profile(child_id, strictness=, age=, household_id=, name=)`, `db.get_child_profile`.
- Produces: `PATCH /v1/children/{child_id}` with the existing `ChildSettingsPayload`. 404 when child not in household.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_children_patch_endpoint.py
import asyncio
import pytest
from fastapi import HTTPException
from watchit_api import main


def _ctx():
    return {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}


def test_patch_child_updates_scoped(monkeypatch):
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid: {"id": cid})
    seen = {}
    def fake_update(child_id, strictness=None, age=None, household_id=None, name=None, **kw):
        seen.update({"child_id": child_id, "household_id": household_id, "age": age})
    monkeypatch.setattr(main.db, "update_child_profile", fake_update)
    monkeypatch.setattr(main.db, "log_audit", lambda *a, **k: None)
    body = main.ChildSettingsPayload(name="Kid", age=10, strictness="high")
    out = asyncio.run(main.patch_child("child_1", body, guardian_ctx=_ctx()))
    assert out["ok"] is True
    assert seen == {"child_id": "child_1", "household_id": "hh_1", "age": 10}


def test_patch_child_404(monkeypatch):
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid: None)
    body = main.ChildSettingsPayload(name="Kid", age=10, strictness="high")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.patch_child("nope", body, guardian_ctx=_ctx()))
    assert exc.value.status_code == 404
```

Note: match the real field names on `ChildSettingsPayload` (read its definition at `main.py:131`); adjust the constructor args if they differ.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src" .venv/bin/pytest tests/test_children_patch_endpoint.py -v`
Expected: FAIL — `AttributeError: module 'watchit_api.main' has no attribute 'patch_child'`.

- [ ] **Step 3: Convert the route**

Change the existing `@app.post("/v1/children/{child_id}/settings")` handler in `main.py` to:

```python
@app.patch("/v1/children/{child_id}")
async def patch_child(child_id: str, body: ChildSettingsPayload, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    if not db.get_child_profile(child_id, household_id):
        raise HTTPException(404, "child not found")
    # preserve the existing update call body (field mapping) from the old handler
    db.update_child_profile(child_id, strictness=body.strictness, age=body.age, household_id=household_id, name=body.name)
    db.log_audit(household_id, "child_settings_updated", guardian_id=guardian_id, entity_type="child", entity_id=child_id)
    return {"ok": True}
```

Keep whatever field mapping the old handler used (e.g. if it also set `timezone` or `set_active_child_id`); only the decorator, function name, and 404 check are the required changes.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src" .venv/bin/pytest tests/test_children_patch_endpoint.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/watchit_api/main.py tests/test_children_patch_endpoint.py
git commit -m "refactor: PATCH /v1/children/{id} replaces POST .../settings"
```

---

### Task 7: `PATCH /v1/control` (replace `POST /v1/control/pause` + `/resume`)

**Files:**
- Modify: `apps/api/src/watchit_api/main.py` (replace the two control routes, ~lines 254-298)
- Test: `tests/test_control_patch_endpoint.py`

**Interfaces:**
- Consumes: `db.set_setting`, `db.delete_setting`, `db.is_parent_pin_set`, `db.verify_parent_pin`.
- Produces: `PATCH /v1/control` with `ControlPatchPayload(paused_until_minutes: int, pin: str | None)`.
  - `> 0` → pause (PIN required, 0/absent minutes → indefinite as today), `== 0` → resume.
  - Returns `{"ok": True, "paused_until": int | None}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_control_patch_endpoint.py
import asyncio
import time
import pytest
from fastapi import HTTPException
from watchit_api import main


def _ctx():
    return {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}


def test_resume_deletes_setting(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.db, "delete_setting", lambda k, hid: seen.setdefault("k", (k, hid)))
    out = asyncio.run(main.patch_control(main.ControlPatchPayload(paused_until_minutes=0), guardian_ctx=_ctx()))
    assert out == {"ok": True, "paused_until": None}
    assert seen["k"] == ("paused_until", "hh_1")


def test_pause_requires_pin(monkeypatch):
    monkeypatch.setattr(main.db, "is_parent_pin_set", lambda hid: True)
    monkeypatch.setattr(main.db, "verify_parent_pin", lambda pin, hid: False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.patch_control(main.ControlPatchPayload(paused_until_minutes=30, pin="0000"), guardian_ctx=_ctx()))
    assert exc.value.status_code == 403


def test_pause_sets_setting(monkeypatch):
    monkeypatch.setattr(main.db, "is_parent_pin_set", lambda hid: True)
    monkeypatch.setattr(main.db, "verify_parent_pin", lambda pin, hid: True)
    saved = {}
    monkeypatch.setattr(main.db, "set_setting", lambda k, v, hid, gid: saved.setdefault("v", (k, v)))
    out = asyncio.run(main.patch_control(main.ControlPatchPayload(paused_until_minutes=30, pin="1234"), guardian_ctx=_ctx()))
    assert out["paused_until"] > int(time.time() * 1000)
    assert saved["v"][0] == "paused_until"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src" .venv/bin/pytest tests/test_control_patch_endpoint.py -v`
Expected: FAIL — `AttributeError: module 'watchit_api.main' has no attribute 'ControlPatchPayload'`.

- [ ] **Step 3: Replace the two routes**

Remove `@app.post("/v1/control/pause")` and `@app.post("/v1/control/resume")`. Add the payload near the others and one PATCH route:

```python
class ControlPatchPayload(BaseModel):
    paused_until_minutes: int
    pin: Optional[str] = None


@app.patch("/v1/control")
async def patch_control(body: ControlPatchPayload, guardian_ctx=Depends(require_guardian)):
    import time
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    if body.paused_until_minutes == 0:
        db.delete_setting("paused_until", household_id)
        log_service_event("monitor_resumed")
        logger.info("monitor_resumed")
        return {"ok": True, "paused_until": None}
    if not db.is_parent_pin_set(household_id):
        raise HTTPException(409, "parent_pin_required")
    if not db.verify_parent_pin(body.pin or "", household_id):
        raise HTTPException(403, "Invalid PIN")
    minutes = body.paused_until_minutes
    horizon_minutes = minutes if minutes > 0 else 10 * 365 * 24 * 60
    until_ms = int(time.time() * 1000 + horizon_minutes * 60 * 1000)
    db.set_setting("paused_until", str(until_ms), household_id, guardian_id)
    log_service_event("monitor_paused", {"minutes_requested": minutes, "paused_until_ms": until_ms})
    logger.info("monitor_paused", minutes_requested=minutes, paused_until_ms=until_ms)
    return {"ok": True, "paused_until": until_ms}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src" .venv/bin/pytest tests/test_control_patch_endpoint.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/watchit_api/main.py tests/test_control_patch_endpoint.py
git commit -m "refactor: PATCH /v1/control replaces POST control/pause+resume"
```

---

### Task 8: Dashboard routing — `children` view, route, nav

**Files:**
- Modify: `apps/dashboard/src/lib/dashboard-model.ts` (add to `DashboardView`)
- Create: `apps/dashboard/src/app/children/page.tsx`
- Modify: `apps/dashboard/src/components/dashboard/dashboard-app.tsx` (nav item + view routing)

**Interfaces:**
- Consumes: existing `DashboardApp({ initialView })`, `AppShell` nav list.
- Produces: reachable `/children` route rendering a placeholder that Task 9 fills.

- [ ] **Step 1: Add the view type**

In `apps/dashboard/src/lib/dashboard-model.ts` change:

```ts
export type DashboardView = "dashboard" | "settings" | "profile" | "children";
```

- [ ] **Step 2: Create the route**

Create `apps/dashboard/src/app/children/page.tsx`:

```tsx
import { DashboardApp } from "@/components/dashboard/dashboard-app";

export default function ChildrenPage() {
  return <DashboardApp initialView="children" />;
}
```

- [ ] **Step 3: Add nav item + route the view**

In `apps/dashboard/src/components/dashboard/dashboard-app.tsx`:
- Import `Users` from `lucide-react` (add to the existing lucide import).
- Add to the nav items list (near line 532):

```tsx
    { view: "children", href: "/children", label: "Children", icon: Users },
```

- In the `initialView === ...` render chain (near line 425-445), add a branch before the default:

```tsx
          ) : initialView === "children" ? (
            <ChildrenView
              children={children}
              authToken={authToken}
              onCreatePairingCode={createPairingCodeForChild}
              onStartMonitoring={startMonitoringForChild}
              onStopMonitoring={stopMonitoringForChild}
            />
```

Task 9 defines `ChildrenView` and the three handlers. For this task, temporarily render `<div>Children</div>` in that branch so the app compiles; Task 9 replaces it.

- [ ] **Step 4: Typecheck**

Run: `cd apps/dashboard && NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_dummy CLERK_SECRET_KEY=sk_test_dummy npm run build`
Expected: build succeeds; `/children` route compiles.

- [ ] **Step 5: Commit**

```bash
git add apps/dashboard/src/lib/dashboard-model.ts apps/dashboard/src/app/children/page.tsx apps/dashboard/src/components/dashboard/dashboard-app.tsx
git commit -m "feat: add /children route, nav item, and view type"
```

---

### Task 9: `children-view.tsx` — children cards, pairing, monitoring, devices

**Files:**
- Create: `apps/dashboard/src/components/dashboard/children-view.tsx`
- Modify: `apps/dashboard/src/components/dashboard/dashboard-app.tsx` (add the three per-child handlers; wire `ChildrenView`)

**Interfaces:**
- Consumes: `apiFetch`, existing UI primitives (`Card`, `Button`, `Badge`, `Input`, `Label`, `Alert`), `ChildProfile` type.
- Produces: `ChildrenView` component; handlers `createPairingCodeForChild(childId)`, `startMonitoringForChild(childId)`, `stopMonitoringForChild(childId, pin)`.
- Device shape from `GET /v1/children/{id}/devices`: `{ id, child_id, device_name, browser_name, status, last_seen_at, paused_until, created_at }`.

- [ ] **Step 1: Add per-child handlers in `dashboard-app.tsx`**

Add alongside the existing `createPairingCode`/`startMonitoring` handlers:

```tsx
  const createPairingCodeForChild = async (childId: string): Promise<string | null> => {
    const data = await apiFetch<{ pairing_code: { code: string } }>("/v1/device/pairing-codes", authToken, {
      method: "POST",
      body: JSON.stringify({ child_id: childId, ttl_minutes: 15 }),
    });
    return data.pairing_code?.code || null;
  };

  const startMonitoringForChild = async (childId: string) => {
    await apiFetch("/v1/monitoring/start", authToken, {
      method: "POST",
      body: JSON.stringify({ child_id: childId }),
    });
  };

  const stopMonitoringForChild = async (childId: string) => {
    await apiFetch("/v1/monitoring/stop", authToken, {
      method: "POST",
      body: JSON.stringify({ child_id: childId }),
    });
  };
```

Replace the temporary `<div>Children</div>` from Task 8 with the real `<ChildrenView .../>` (props as in Task 8 Step 3). Import `ChildrenView` at the top.

- [ ] **Step 2: Create the component**

Create `apps/dashboard/src/components/dashboard/children-view.tsx`:

```tsx
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
  authToken: string | null;
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

function ChildCard({ child, authToken, onCreatePairingCode, onStartMonitoring, onStopMonitoring }: { child: ChildProfile } & Props) {
  const [code, setCode] = useState<string | null>(null);
  const [devices, setDevices] = useState<DeviceRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadDevices = async () => {
    setError(null);
    try {
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
              <DeviceRowView key={d.id} device={d} authToken={authToken} onChanged={loadDevices} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function DeviceRowView({ device, authToken, onChanged }: { device: DeviceRow; authToken: string | null; onChanged: () => Promise<void> }) {
  const [minutes, setMinutes] = useState("30");
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pausedActive = device.paused_until != null && device.paused_until > Date.now();

  const patch = async (paused_until_minutes: number, withPin: boolean) => {
    setBusy(true);
    setError(null);
    try {
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
```

Note: confirm `ChildProfile` exposes `name`, `age`, `strictness` (it is the `/v1/children` row). If a field name differs, adjust the JSX accessors.

- [ ] **Step 3: Typecheck / build**

Run: `cd apps/dashboard && NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_dummy CLERK_SECRET_KEY=sk_test_dummy npm run build`
Expected: build succeeds with no type errors.

- [ ] **Step 4: Commit**

```bash
git add apps/dashboard/src/components/dashboard/children-view.tsx apps/dashboard/src/components/dashboard/dashboard-app.tsx
git commit -m "feat: children view with pairing, monitoring, and per-device pause"
```

---

### Task 10: Point existing dashboard clients at the new PATCH routes

**Files:**
- Modify: `apps/dashboard/src/components/dashboard/dashboard-app.tsx` (child-settings calls ~lines 256, 268; pause ~line 314; resume ~line 336)

**Interfaces:**
- Consumes: `PATCH /v1/children/{id}`, `PATCH /v1/control` (Tasks 6, 7).

- [ ] **Step 1: Update child-settings calls**

Change both `apiFetch(\`/v1/children/${...}/settings\`, ..., { method: "POST", ... })` sites (create-child at ~256 and update-child at ~268) to:

```tsx
    await apiFetch(`/v1/children/${encodeURIComponent(childId)}`, authToken, {
      method: "PATCH",
      body: JSON.stringify({ /* same body as before */ }),
    });
```

Keep each call's existing JSON body unchanged; only the URL (`/settings` removed) and `method` (`PATCH`) change.

- [ ] **Step 2: Update pause call (~line 314)**

```tsx
      const data = await apiFetch<{ ok: boolean; paused_until: number | null }>("/v1/control", authToken, {
        method: "PATCH",
        body: JSON.stringify({ paused_until_minutes: minutes, pin }),
      });
```

Use the same `minutes` and `pin` variables the old `/v1/control/pause` body used.

- [ ] **Step 3: Update resume call (~line 336)**

```tsx
    await apiFetch("/v1/control", authToken, {
      method: "PATCH",
      body: JSON.stringify({ paused_until_minutes: 0 }),
    });
```

- [ ] **Step 4: Build**

Run: `cd apps/dashboard && NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_dummy CLERK_SECRET_KEY=sk_test_dummy npm run build`
Expected: build succeeds.

- [ ] **Step 5: Commit**

```bash
git add apps/dashboard/src/components/dashboard/dashboard-app.tsx
git commit -m "refactor: dashboard uses PATCH for child settings and control pause"
```

---

### Task 11: Full verification

- [ ] **Step 1: Run the suite**

Run: `make verify`
Expected: compile + pip check + single alembic head + pytest (all new tests pass) + dashboard typecheck all green.

- [ ] **Step 2: Manual smoke (local)**

Start `make run-api` and `make run-dashboard`. Visit `/children`: each child shows name/age/strictness; Pair shows a code; View devices lists paired devices; Play/Stop toggle monitoring; per-device Pause (with PIN) sets a paused badge; Resume clears it. Confirm a paused device's visits record as `allow` and auto-resume after the window.

- [ ] **Step 3: Commit any fixups**

```bash
git add -A && git commit -m "test: verify children & monitoring page end to end"
```
```

---

## Self-review notes

- Spec coverage: devices list (T2-3), per-device pause (T1,2,4,5), PATCH children (T6), PATCH control (T7), page/route/nav (T8), UI (T9), client migration (T10), verification (T11). All spec sections covered.
- Overrides intentionally left POST (not in any task) — correct per spec.
