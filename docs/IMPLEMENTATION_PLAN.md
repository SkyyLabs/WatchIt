# Implementation Plan — First Vertical Slice

Scope: Phase 0 + Phase 1 of `PRODUCT_ROADMAP.md`, shipped as one reviewable branch off `dev`.
Each slice below lists the user problem, touched surfaces, and acceptance criteria. All slices
keep `make verify` green; schema changes are one new Alembic revision (single head preserved).

## Slice 1 — Security corrections + token lifecycle

**User problem:** a family's data must not be writable/readable by another family or by a
brute-forced pairing; a stolen token must age out.

- Schema (`20260707_0006`): `devices.token_expires_at`, `devices.token_prev_hash`,
  `devices.token_rotated_at`, `devices.policy_fetched_at`; `policy_rules` table (slice 4 uses
  it) with indexes.
- `db.py`: expiry-aware `authenticate_device_token` (distinct `revoked` / `expired` outcomes),
  `rotate_device_token` (1 h grace for previous hash), `get_event_household`.
- API: structured 401 bodies (`device_revoked` / `token_expired`); `/v1/event/upgrade`
  verifies event ∈ device household (403 otherwise); in-process rate limiter on
  `/v1/device/redeem` (per-IP + global, 429, audit row on failure);
  `POST /v1/device/token/rotate`.
- SSE: device stream filtered by `device_id`.
- Worker: LLM judge failure fallbacks become `warn/system_uncertain` (never allow); policy
  engine + headlines agent switch to exact-suffix domain matching.
- Risks: none to existing data; token expiry only applies to newly issued/rotated tokens
  (existing rows have NULL = no expiry until next rotation/re-pair).

**Accepted when:** wrong-household upgrade returns 403; redeem floods return 429 + audit;
expired token yields `token_expired`; rotation works and old token survives ≤ 1 h;
`alphabet.com` is not blocked; LLM failure yields `warn` with `system_uncertain` category.

## Slice 2 — Device health & policy freshness

**User problem:** "is this device actually protected right now?"

- `GET /v1/device/policy` stamps `policy_fetched_at`; `fetch_devices` returns it.
- Dashboard device rows show health: healthy / policy-stale / offline / paused / revoked,
  derived from `last_seen_at` + `policy_fetched_at`.

**Accepted when:** device list shows freshness state without extra clicks.

## Slice 3 — Versioned policy snapshot

**User problem:** the extension needs an offline-capable, versioned view of "what the rules are
for this child on this device".

- `db.build_policy_snapshot(household, child, device)`: scope ids, policy version, content-hash
  `version`, `issued_at`/`expires_at` (24 h), `refresh_after_seconds` (900), monitoring +
  pause + device status, strictness, manual rules, quiet-hours windows, up to 200
  high-confidence cached URL decisions, `token_expires_at`.
- API `GET /v1/device/policy` (device auth).
- Extension: fetch on startup + `chrome.alarms` (15 min) + after pairing; stored in
  `chrome.storage.local`; freshness state machine per
  `LOW_LATENCY_ENFORCEMENT_DESIGN.md`.

**Accepted when:** snapshot is household/child/device-scoped (tested), versioned (hash stable
for identical content), and the extension persists + refreshes it.

## Slice 4 — Guardian manual rules, enforced everywhere

**User problem:** "always block roblox.com for Sam", "always allow school portal", instantly,
without waiting for AI.

- `policy_rules`: id, household_id, child_id (nullable), device_id (nullable), action
  (allow|block), rule_type (domain|url|prefix), pattern, reason, created_by_guardian_id,
  expires_at, enabled, timestamps. Precedence: device > child > household; block beats allow at
  equal scope; url/prefix rules beat domain rules at equal scope.
- `watchit_core.policy.rules.evaluate_rules(...)` — one shared implementation used by the
  worker gate (before the URL cache) and mirrored in extension JS.
- API: `GET/POST /v1/rules`, `DELETE /v1/rules/{id}` (guardian auth, household-scoped,
  audited).
- Dashboard: rules manager on the child page (list, add domain/URL rule with action + optional
  reason, delete); shows scope and creator.
- Extension: local evaluation before any network wait.

**Accepted when:** rule changes appear in the next snapshot; worker decisions carry
`manual_rule:<id>` reason; precedence covered by unit tests; UI can create/list/delete.

## Slice 5 — State-specific enforcement in the extension

**User problem:** every page currently sits behind a 20 s spinner, then fails open.

- `content.js`: no universal loader. Local allow → render; local block → instant interstitial
  (reason: rule/quiet-hours/cached); unknown → render + small checking badge + apply backend
  decision on arrival; unknown-high-risk (snapshot token list) → loader ≤ 5 s then blur+warn
  until decided; degraded (expired snapshot & backend unreachable) → banner.
- `background.js`: local layers evaluated at `onCommitted` using the stored snapshot; event
  still posted (with local decision annotation when short-circuited); `eventIdByTab` moved to
  `chrome.storage.session` so SW eviction doesn't orphan the poll; revoked/expired auth
  responses clear pairing state.

**Accepted when:** blocklisted-by-rule page never renders content; normal unknown page renders
immediately; backend-down browsing shows degraded banner and still enforces local rules; SW
restart mid-navigation still resolves the decision.

## Slice 6 — Tests

pytest (unit, monkeypatched db, consistent with the existing suite):
pairing redeem throttling; revoked + expired token auth; rotation grace; wrong-household
upgrade 403; rules precedence matrix; snapshot content/scoping/version stability; stale-claim
semantics documented; LLM invalid-output and call-failure fallbacks; device SSE filtering;
suffix matching (allow/block lists, headline tokens).
Extension logic that must be testable (rule evaluation) mirrors the Python tests' fixture
table; JS test harness is deferred (no JS test infra in repo — noted, not hidden).

## Rollout / rollback

- One migration, additive only (new table + nullable columns) — forward-safe, no backfill.
- Extension changes are behaviorally guarded by snapshot presence: without a snapshot it
  behaves like today (hold + poll), so a stale extension against a new API keeps working, and
  the new extension against an old API degrades to current behavior (404 on
  `/v1/device/policy` → no snapshot → legacy path).
- Rollback = revert branch; migration can stay (unused table/columns are inert).
