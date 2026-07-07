# Product Roadmap

North star: a guardian signs in to a hosted dashboard, pairs the extension on each of the
family's browsers in under a minute, and from then on obvious safety decisions are instant and
local, AI handles only the ambiguous residue, and the dashboard answers "is everyone protected
and what needs my attention" at a glance.

## Phase 0 — Security corrections (immediate, no product change)
- Scope `/v1/event/upgrade` to the device's household (cross-tenant write fix).
- Throttle + audit `/v1/device/redeem`.
- Filter the device SSE stream by device.
- Exact-suffix domain matching in policy/headline layers (kills `alphabet.com`-class false
  blocks that get cached for a day).
- LLM failure fallback: `warn/system_uncertain`, never `allow`.

## Phase 1 — Local enforcement foundation (this branch's vertical slice)
- Device-token expiry + rotation with grace window; structured `device_revoked` /
  `token_expired` auth errors.
- Device health: `policy_fetched_at` + `last_seen_at` surfaced per device in the dashboard.
- Versioned policy snapshot endpoint (`GET /v1/device/policy`).
- Guardian manual allow/block rules (`policy_rules` table, CRUD API, dashboard management UI),
  enforced server-side in the worker and locally in the extension.
- Extension: snapshot fetch + storage + freshness states; local layers (device state → rules →
  quiet hours → cached decisions); loader only for high-risk-unknown pages (5 s cap → blur),
  everything else renders with async decision application.
- Tests across pairing, revocation, wrong-household access, rules precedence, snapshot scoping,
  stale policy, LLM-failure fallback.

## Phase 2 — Protection by default & failure visibility
- Pairing implies protection: auto-start a monitoring session at redeem; sessions become audit
  records, not gates (kill the 409-until-guardian-clicks-start behavior).
- Queue hardening: stale-`processing` reaper, attempt caps, dead-letter surface, `/healthz`.
- Decision states carry provenance (`offline_policy_applied`, `protection_degraded`,
  `system_uncertain`, `review_pending`) end-to-end so the dashboard can show protection gaps
  honestly.
- Dashboard "protection status" header: per-device healthy/stale/offline/paused/revoked.

## Phase 3 — Calm command center
- Home = status + review queue (pending OCR, low-confidence, system_uncertain, guardian rules
  suggested by repeated overrides), not an activity feed.
- Decision explanations: which layer decided, rule id / cache / LLM rationale, one-click
  "always allow / always block" that mints a manual rule.
- Rule tester ("what would happen for this URL for this child right now").
- Alerting: email/push for high-severity blocks and protection-degraded transitions.

## Phase 4 — Scale & hosting
- Shared SSE bus (Postgres LISTEN/NOTIFY first — no new infra), multi-instance API.
- Hosted deployment story (containers, config via env, extension `config.js` build step per
  environment, CORS/origin config).
- Rate limiting moves from in-process to shared store.
- Retention sweeps + guardian privacy controls (see `PRIVACY_LOGGING_AND_RETENTION.md`).

## Phase 5 — Judgment quality
- Multimodal judge (screenshot straight to Claude) replacing Docling in the hot path.
- Per-household learned rules with guardian review (learning loop graduates from prompt
  guidance to suggested `policy_rules`).
- Category-aware strictness (e.g. social media time windows vs. hard blocks).

## Explicit non-goals for now
- Mobile / non-Chromium platforms.
- Network-level (DNS) enforcement.
- RAG/vector features (pgvector stays unused by policy).
