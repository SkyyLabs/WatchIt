# Current `dev` Architecture (as of commit d887113)

This document records what actually runs on the `dev` branch today, verified against current
source, migrations, tests, and CI. File references are to the current tree. Nothing here is
aspirational; see `CURRENT_DEV_GAP_ANALYSIS.md` for what is missing or broken.

## 1. Deployables and processes

| Component | Path | Runtime | Entry point |
| --- | --- | --- | --- |
| Event API | `apps/api/src/watchit_api/` | FastAPI + uvicorn, `127.0.0.1:4849` | `watchit_api.main:app` (`make run-api`) |
| Guardian dashboard | `apps/dashboard/` | Next.js App Router, `:4848` | `npm run dev` (`make run-dashboard`) |
| Browser extension | `apps/browser-extension/` | Chromium MV3, dependency-free plain JS | `manifest.json` |
| Agent worker | `services/agent-worker/src/watchit_agents/` | asyncio loop | embedded in API by default (`WATCHIT_EMBEDDED_AGENT_WORKER=true`) or `python -m watchit_agents.worker` |
| Learning worker | `services/learning-worker/src/watchit_learning/` | asyncio loop | **always embedded in the API process** (`main.py` startup); there is no standalone entry point or Make target |
| Shared core | `packages/core/src/watchit_core/` | library | config, DB repository, policy engine, url cache, screenshot store, logging, migrations |

Postgres (Neon) is the only datastore. The job queue is the `event_jobs` table
(`FOR UPDATE SKIP LOCKED` claim in `db.claim_event_jobs`), not an external broker. The SSE
decision bus (`watchit_agents.runtime.DecisionBus`) is an **in-process set of asyncio queues** —
it only works when API and worker share one process. Running the worker out-of-process today
means SSE pushes never reach API subscribers; only the extension's polling path still works.

There are no Dockerfiles, no deployment manifests, and no hosting configuration anywhere in the
tree. CI (`.github/workflows/ci.yml`) runs compile check, `pip check`, single-Alembic-head check,
pytest, dashboard typecheck + build. `make verify` mirrors this locally.

## 2. Request flow (the load-bearing path)

1. **Navigation observed.** `background.js` listens to `webNavigation.onCommitted` (top frame,
   `http(s)` only, skips WatchIt's own hosts from `config.js` `skipHosts`). It samples up to
   4000 chars of `document.body.innerText` via `chrome.scripting.executeScript`.
2. **Ingest.** It POSTs `/v1/event` with the device token (`authorization: Bearer wdev_…`).
   `child_id` in the payload is a placeholder (`"paired"`); the API **overwrites household/child/
   device from the authenticated device row** and rejects with 409 when no active
   `monitoring_sessions` row matches (guardian must have pressed "Start monitoring"). The
   household `monitoring_enabled` switch is snapshotted into the event at ingest time.
3. **Queue.** `db.enqueue_event_job` writes `event_jobs` (`status=pending`), returns
   `{status: queued, job_id, event_id}` immediately. (`WATCHIT_PROCESSING_MODE=sync` runs the
   pipeline inline instead — dev convenience.)
4. **Page hold.** `content.js` (runs at `document_start` on every page when a device token
   exists) paints a full-screen loader immediately and polls the service worker every 1 s
   (`watchit_get_decision` message → SW does the authed `GET /v1/event/{id}/decision`). Hard
   cap 20 s (`POLL_MAX_MS`), then **fail-open**: loader clears, page usable, no decision applied.
   SSE (`/v1/device/stream/decisions?token=…`) is a fast path when the SW is alive; decisions
   are matched to tabs by `tab_id` or URL prefix.
5. **Worker pipeline** (`runtime.process_event`, claimed by `worker.AgentWorker` in batches of
   5, 0.5 s poll):
   - **Pause / monitoring gate**: household `paused_until`, device `paused_until`, household
     monitoring switch → decision `allow` with reason `paused` / `monitoring_disabled`
     (deliberate: browsing is unmonitored, not blocked).
   - **Quiet-hours gate**: `child_schedules` rows (device-scoped rows override child-level
     rows entirely; every enabled window is evaluated in the schedule's/child's timezone) →
     decision `block`, reason `quiet hours`.
   - **Profile load**, then **URL decision cache** lookup (`url_decision_cache`, key =
     sha256(normalized_url | child | strictness | age | policy_version), TTL 86400 s default,
     household-scoped). Hit → replay cached action, done.
   - **LangGraph** (`graph.py`, deterministic conditional edges, no LLM planner):
     `headline` (regex keyword scores + token/domain heuristics; decisive only at
     confidence ≥ 0.85) → `url_llm` (LLM judge over URL/title/DOM sample) → optionally `ocr`
     (Docling over screenshots) → `policy`. Upgrade events enter at `ocr` directly.
   - **Decision + analysis writes**, then publish on the in-memory bus. Interim decision when
     a screenshot is needed: `warn` with reason `pending_ocr` (never `allow`).
6. **OCR upgrade.** When a decision carries `needs_ocr`, the SW captures
   `chrome.tabs.captureVisibleTab` (PNG, base64) and POSTs `/v1/event/upgrade` with the same
   event id; the job re-enqueues with `upgrade=true` and re-runs from the `ocr` node.
7. **Dashboard.** Reads history over HTTP (`/v1/decisions`, `/v1/events`, limit 50) and
   subscribes to `/v1/stream/decisions` (Clerk token + household in query string). Guardian
   overrides POST `/v1/decisions/{id}/override`; each override is a new `decision_overrides`
   row, republished on the bus, written into the URL decision cache (confidence 1.0), and fed
   to the learning loop immediately.

## 3. Auth model

Two disjoint schemes, both in `apps/api/src/watchit_api/auth.py`:

- **Guardian (Clerk).** `require_guardian` parses `Authorization: Bearer <clerk JWT>` via JWKS
  (`CLERK_JWKS_URL` / derived from `CLERK_ISSUER`; audience not verified). First sign-in creates
  the guardian row and either claims the unclaimed `hh_legacy` household or creates a fresh one.
  The resolved `{guardian, household}` is cached in-process per Clerk subject for 300 s.
  Active household selection: `X-Household-Id` header (or `household` query param on the SSE
  route), validated against the guardian's memberships each request; unknown ids silently fall
  back to the default household. SSE uses `require_guardian_stream` with the Clerk token in the
  query string (EventSource cannot set headers).
- **Device (paired extension).** `require_device` / `require_device_stream` resolve an opaque
  token `wdev_<43 chars urlsafe>` by SHA-256 hash against `devices.token_hash` where
  `status='active'`; the lookup also bumps `last_seen_at`. Household/child/device scope always
  derives from the device row, never from the payload. The SSE variant takes the **durable
  device token in the query string**.

### Pairing / token lifecycle as implemented

- Guardian POSTs `/v1/device/pairing-codes` (child-scoped, TTL default 15 min) → 6-digit
  numeric code, stored as SHA-256 hash, returned once.
- Extension POSTs `/v1/device/redeem` (**unauthenticated**; the code is the only secret) with a
  client-generated `installId` (random UUID persisted in `chrome.storage.local`). Redemption is
  atomic single-use (`UPDATE … WHERE redeemed_at IS NULL AND expires_at > now()`).
- Device row is **upserted by `install_id`**: re-pairing the same browser profile rotates the
  token, reactivates the device, and can move it to another child/household ("reassignment");
  the previous child's device-scoped monitoring session is stopped and both households get
  `device_reassigned` audit rows.
- Token is stored raw in `chrome.storage.local`, hash-only server-side. It has **no expiry and
  no rotation** except through re-pairing. Revocation (`DELETE /v1/devices/{id}`) sets
  `status='revoked'`, which kills authentication on the next request.
- Device pause (`PATCH /v1/devices/{id}`) requires the parent PIN and sets `paused_until` (ms);
  the worker's pause gate honors it (pause ⇒ events are allowed through unanalyzed).

## 4. Database model

Migrations: 5 revisions, single head `20260706_0005`. Tables (all household-scoped, no
`watchit_` prefix): `guardians`, `households`, `household_members`, `children`, `devices`,
`device_pairing_codes`, `monitoring_sessions`, `events`, `event_jobs`, `analysis`, `decisions`,
`decision_overrides`, `url_decision_cache`, `child_schedules` (now with nullable `device_id`),
`household_settings` (JSONB key/value: `parent_pin` PBKDF2 record, `monitoring_enabled`,
`paused_until`, `active_child_id`, `guardian_feedback`), `guardian_feedback` (table exists but is
**unused** — the learning loop writes to `household_settings` instead), `screenshot_files`,
`audit_log`, `policy_versions` (table exists, **never written or read**; the live policy version
is the `WATCHIT_POLICY_VERSION` env string), `settings_legacy`.

`packages/core/src/watchit_core/db.py` (~1280 lines) is the single repository layer; psycopg3
pool (1–10 conns), `dict_row`, connection check on checkout. Legacy fallback `hh_legacy`
defaults still exist on many write paths.

## 5. Policy logic

`packages/core/src/watchit_core/policy/engine.py`:

- Hardcoded allowlist `{wikipedia.org, khanacademy.org, .edu}` and blocklist
  `{pornhub.com, xvideos.com, redtube.com}`, both matched by **substring containment** against
  the domain.
- Strictness block thresholds over regex keyword scores (`SafetyAnalyzer`): lenient 0.95 /
  standard 0.90 / strict 0.80.
- Headline `risk=high` → block. Otherwise the LLM judge's action passes through
  (allow/block/blur; `warn`/`notify` are coerced to **block** — "not enforced yet").
- Final fallback: `default allow` (documented as unreachable for real pages because
  `judge_json` is always populated before `policy`).

Quiet hours are enforced in the worker gate, not in `PolicyEngine.decide`. The env schedule
settings (`WATCHIT_SCHEDULE_*`) still exist in `config.py` but are only referenced by a test
fixture; the live path uses `child_schedules` rows.

The LLM judge (`llm_judge.py`) prompts for strict JSON (`is_harmful, categories, severity,
rationale, action, confidence`), validates with Pydantic, and has two failure fallbacks, both
`action=allow` (confidence 0.0 for call failure, 0.2 for unparseable output). Provider selection
(`llm_provider.py`): `ollama` (default), `anthropic`, or `openai`-compatible, with Ollama
configured as automatic fallback for both cloud providers. Guardian-override guidance from the
learning loop is appended to the system prompt when present.

## 6. OCR flow

`ocr_asr.py` uses **Docling** (`DocumentConverter`) with a project-local model cache. Up to 3
screenshots per event, each round-tripped through a temp file. OCR text is fed back into a
second LLM judge pass. When `WATCHIT_SAVE_SCREENSHOTS=true`, raw PNGs are written under
`screenshots/YYYY-MM-DD/{event_id}/` with a `metadata.json` (url, title, ids) and a
`screenshot_files` row (path, sha256, size; `ocr_text` column exists). Postgres never stores
image bytes. Extension-side dedup of upgrades is an in-memory `upgradedEvents` set in the
service worker.

## 7. Dashboard

Next.js App Router, all feature UI client-rendered behind Clerk (`proxy.ts` middleware). One
data provider (`dashboard-data.tsx`) fetches households/children/security once, activity
(50 decisions + 50 events) lazily, and holds the SSE subscription. Routes: `/` (metrics,
attention list, activity timeline, category breakdown, controls), `/children` (profiles,
per-child devices with pause/revoke, pairing-code dialog, quiet-hours editor), `/household`
(household switcher/create, monitoring master switch, child move), `/settings` (PIN, pause),
`/profile`. Client logs ship to `POST /v1/client-logs`. "Safety score" and category breakdown
are heuristic client-side computations over the last 50 rows.

## 8. Logging & observability

`watchit_core.logging`: structlog → JSON lines, async queue listener, per-request context
(request id, household, child, device, event, job ids, timings). `activity_logger.py`
additionally writes a per-day JSONL session log (`logs/`) including `log_step("event_received",
event, …)` payloads and `llm_raw_response` entries (raw model output, truncated to 2000 chars).
Per-step pipeline timings are logged at DEBUG; one INFO `decision` line per decision.

## 9. Test coverage (what is actually tested)

15 pytest files, all unit-level with monkeypatched `db` — **no test touches a real Postgres**.
Covered: policy engine invariants, quiet-hours window math + timezone evaluation, url cache
normalization/keying, graph routing (incl. false-negative guards: uncertain-never-allow,
pending-OCR-is-warn), schedule endpoint validation, household selection scoping, child move
authorization, device pause/revoke repository scoping, device SSE auth, runtime pause gate,
children CRUD endpoints. Not covered: pairing/redeem lifecycle, token auth end-to-end, ingest
409 path, upgrade path, worker claim/retry, SSE fan-out, learning loop, extension JS (no JS
tests at all), dashboard components.

## 10. Deployment assumptions baked into the code

- Extension `config.js` hardcodes `apiBase: http://127.0.0.1:4849`; `skipHosts` are localhost
  ports. Manifest requests `<all_urls>` host permissions.
- API CORS origins come from `WATCHIT_CORS_ORIGINS` (default: local dashboard).
- SSE bus is in-memory by default (`WATCHIT_SSE_BUS=memory`); `postgres` mode fans decisions
  out via pg_notify so multiple API instances and a standalone worker deliver to SSE.
- Screenshots and Docling caches on local disk of the worker process.
- `/v1/device/redeem` is throttled via a Postgres-backed sliding window
  (`rate_limit_hits`), so limits hold across API replicas. `/healthz` exists.
- No request size limits, no TLS assumptions, no metrics endpoint.
