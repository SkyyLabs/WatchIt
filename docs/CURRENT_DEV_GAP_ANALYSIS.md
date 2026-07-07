# Current `dev` Gap Analysis (as of commit d887113)

Every finding below is grounded in the current source; file:line references are to the `dev`
tree. Ordered by risk to a real family using the product, not by effort.

## A. Security vulnerabilities (fix before anything else)

### A1. `/v1/event/upgrade` writes cross-household — HIGH
`main.py` `post_event_upgrade` accepts a client-supplied `id` (event id) and the pipeline calls
`db.update_event_data_json(event_id, …)` (`runtime.py:221`) with **no household scope**. A
hostile paired device (any household) can overwrite `data_json`/`raw_json` of any event in any
household by guessing/replaying event ids, inject screenshots into another family's event, and
trigger a fresh decision + SSE publish for that family. Fix: verify the event exists and belongs
to the device's household before enqueueing the upgrade.

### A2. Unauthenticated pairing redeem with no rate limit — HIGH
`POST /v1/device/redeem` is unauthenticated (by design — the code is the secret) but the code
space is 10^6 and there is **no rate limit, no per-code attempt counter, no IP throttle**
anywhere in the API. Sweeping the space while any pairing code is outstanding is practical
(~hours at modest request rates); success silently pairs an attacker device into the family and
streams the household's decisions (URLs, titles) via SSE. Fixes: per-code and per-IP attempt
limits, constant-time failure, longer code or code+household binding, alerting on redemption
failures, and short default TTL (already 15 min — keep).

### A3. Durable device token in SSE query string — MEDIUM
`GET /v1/device/stream/decisions?token=wdev_…` (`auth.require_device_stream`) puts the
**long-lived** device credential in a URL: proxy logs, browser internals. The guardian
stream has the same shape but Clerk tokens are short-lived (~60 s), which is acceptable;
the device token is forever. Fix: short-lived stream ticket minted over an authenticated POST,
or move device delivery to polling-only.

### A4. Device SSE stream leaks the whole household — MEDIUM
`sse_generator` filters by `household_id` only. A device paired to child A receives decisions
for child B's devices (URLs and titles of siblings' browsing) and could apply them to matching
tabs cross-device (`background.js` URL-prefix fallback match). Fix: filter device streams by
`device_id` (or at least `child_id`).

### A5. Clerk JWT audience not verified — LOW
`verify_clerk_token` sets `verify_aud: False`. Tokens minted for other Clerk apps under the same
issuer would pass. Verify `azp`/`aud` against configured origins.

### A6. Guardian household cache TTL vs revocation — LOW
`_household_cache` caches guardian→household for 300 s in-process; removing a guardian from a
household leaves their access live up to 5 min per API instance. Acceptable short-term; document
or bust the cache on membership change.

## B. Enforcement model gaps (the product problem)

### B1. Universal 20-second remote wait, then fail-open
`content.js` holds every page behind a loader for up to 20 s (`POLL_MAX_MS=20000`) and then
**fails open silently** — this is both the worst possible UX for the safe-page majority and not
safe for the unsafe minority (LLM outage, worker down, API down, offline ⇒ everything is allowed
after 20 s with no signal to guardian or child). There is no local policy, no local cache, no
distinction between "backend slow" and "backend gone".

### B2. No local decision layers at all
The extension has zero local intelligence: no policy snapshot, no manual allow/block rules, no
schedule awareness, no cached decisions, no offline behavior. Even `pornhub.com` — on the
hardcoded server blocklist — renders for up to 20 s before the block lands (or forever if the
backend is down). Quiet hours are enforced only server-side, so an offline device ignores them.

### B3. Service-worker state loss breaks the decision path
`eventIdByTab`, `eventContextByTab`, `upgradedEvents` live in SW memory (`background.js`).
MV3 eviction between POST and poll loses the event id ⇒ content script polls "pending" until the
20 s fail-open. No persistence to `chrome.storage.session`, no recovery.

### B4. Monitoring requires a manual guardian "start" per child
Ingest 409s unless a guardian started a `monitoring_sessions` row from the dashboard. A paired
device is *unprotected by default* until someone clicks Start monitoring, and stays unprotected
silently after a "stop". For a safety product, pairing should imply protection; sessions should
be an audit concept, not a gate.

### B5. Failure semantics are one-size-fits-all
- LLM call failure → `action=allow, confidence 0.0` (`llm_judge.py:150-158`); unparseable LLM
  output → `allow, 0.2`. Combined with B1, model outage = unrestricted browsing.
- Worker crash mid-job leaves `event_jobs.status='processing'` forever — there is **no retry,
  no stale-claim reaper, no dead-letter**; `attempts` is incremented but never limited or used.
- `failed` jobs are terminal and invisible (no endpoint, no dashboard surface).
- No distinction between allow / offline-allow / degraded-allow anywhere in the data model; the
  dashboard cannot tell a guardian "protection was down between 3pm and 4pm".

### B6. Headline/policy list matching is substring-based
`HIGH_RISK_TOKENS` uses `token in domain or token in title` (`headlines_agent.py:47`):
`"bet" in "alphabet.com"` → high-risk **block** at 0.9 confidence, which is also **written into
the URL decision cache** (0.9 ≥ 0.85 threshold) and replayed for a day. `.edu` allowlist matches
`*.education` domains; `wikipedia.org` substring matches `evilwikipedia.org.attacker.com`-style
hosts. Needs proper eTLD+1/suffix matching.

## C. Pairing / device lifecycle gaps (vs. product goal #2)

- **No token expiry or rotation** — a stolen `chrome.storage.local` token works forever until a
  guardian manually revokes the device.
- **Revocation is silent client-side** — the extension gets 401s, swallows them
  (`catch(_)`), keeps the loader/poll cycle, and fails open. No `device_revoked` state, no
  user-visible "this device is no longer protected", no guardian alert that a revoked device is
  still browsing.
- **Unpair/re-pair races**: same `install_id` upsert reassigns silently; the stolen-device
  recovery story is "revoke and hope the child doesn't clear extension storage" (clearing
  storage just unpairs — protection off, nobody notified).
- **No device health**: `last_seen_at` is bumped on every authenticated call but never surfaced;
  no heartbeat, no "stale device" concept, no policy-freshness tracking (there is no policy to
  be fresh — see B2).
- **No pairing-code lifecycle UI**: codes can't be listed or cancelled; unredeemed codes for a
  moved child are transferred (good) but a guardian can't see outstanding codes.
- **Audit** exists for pair/reassign/revoke/pause (good foundation) but not for token auth
  failures, redemption failures, or pairing-code cancellation.

## D. Rules & policy management gaps (vs. product goal #4)

- Allow/block lists are **hardcoded Python sets** (`policy/engine.py:47-48`) shared by every
  household on the planet. No guardian CRUD, no household/child/device scoping, no
  URL-vs-domain-vs-pattern distinction, no expiry, no reason, no creator, no audit, no
  precedence rules, no UI. Overrides partially compensate (they seed the URL cache per
  child+URL) but are reactive, TTL-bound (86400 s), invalidated by any strictness/age/policy
  version change, and invisible as a managed rule set.
- `policy_versions` table exists but nothing writes/reads it; the effective "policy version" is
  an env string, so cache keys can silently collide across genuinely different policies deployed
  with the same version string.

## E. OCR pipeline gaps (vs. product goal #6)

- **No payload limits**: `/v1/event/upgrade` accepts arbitrary base64 in `data_json` (full JSON
  body, no size cap configured), which lands in `event_jobs.event_json` — multi-MB rows in
  the queue table per screenshot.
- **Dedup only in SW memory**: `upgradedEvents` dies with the worker; server side will happily
  re-enqueue repeated upgrades for the same event.
- **No retention/deletion**: `screenshot_files.retention_expires_at` column exists, nothing
  populates or enforces it; no delete endpoint; `screenshots/` directories accumulate forever.
  Note: `screenshots/evt_*/metadata.json` files (with URLs/titles) are **committed to the git
  repo** — real browsing metadata in version control.
- **`ocr_text` column** stores extracted page text in Postgres indefinitely (privacy).
- **Docling suitability**: heavyweight document-conversion stack (HF model downloads, torch)
  used for single browser screenshots; first-call model download can take minutes; latency per
  screenshot is seconds. No timeout around `convert`, no metric, no failure counter; exceptions
  are swallowed to empty string (`ocr_agent.extract_text`), which silently degrades to a
  no-OCR re-judge.
- **`llm_raw_response` and event payloads (DOM samples) are written to the activity log**
  (`activity_logger.log_step`, `llm_judge.py:145`) — conflicts with "never log full sensitive
  payloads".

## F. Dashboard gaps (vs. product goal #7)

- Home view is an activity feed + heuristic "safety score" (client-computed over the last 50
  rows, arbitrary weights) — not "is protection active / what needs attention".
- No device-health surface (last seen, stale, revoked, unpaired), no protection-state banner,
  no review queue (pending_ocr / failed jobs / low-confidence decisions), no rules UI, no
  decision-explanation drill-down beyond a reason string, no notification/alerting concept.
- SSE-only live updates with no polling fallback in the dashboard (extension has one; the
  dashboard `onerror` just logs a warning; EventSource auto-reconnects with the **same possibly
  expired Clerk token** in the URL, so streams die permanently after ~60 s token expiry on
  reconnect).

## G. Platform gaps

- **Scaling**: in-memory SSE bus pins deployment to one API process with embedded worker;
  no shared bus (Redis/pg LISTEN-NOTIFY). Learning loop always runs inside the API.
- **Ops**: no `/healthz`, no metrics, no job-queue depth visibility, no dead-letter view, no
  rate limiting, no request size limits.
- **Config**: extension API base is a hardcoded constant edited by hand; no environment story
  for a hosted API; CORS pinned to localhost.
- **Docs drift**: `CLAUDE.md`/rules describe endpoints that don't exist on dev
  (`POST /v1/children/{id}/settings`, `POST /v1/control/pause`/`resume` — actual routes are
  `PATCH /v1/children/{id}` and `PATCH /v1/control`) and omit the device SSE stream and event
  decision-poll endpoints. `guardian_feedback` and `policy_versions` tables are dead schema.
- **Tests**: no integration path (real Postgres, real HTTP), no extension tests, no coverage of
  pairing lifecycle, upgrade scoping, or failure semantics — i.e., none of the paths in section
  A/B are guarded by tests today.

## Priority order (informs the roadmap)

1. A1, A2 (exploitable), B6 (wrong blocks cached for a day)
2. B1–B5 with the policy-snapshot + local-rules design (product goal #1/#3/#5)
3. C (token lifecycle, health, revocation UX)
4. D (guardian rules + UI)
5. E (OCR hardening), F (dashboard command center), G (platform)
