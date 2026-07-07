# Low-Latency Enforcement Design

Goal: obvious decisions happen instantly and locally; the backend (LLM/LangGraph/OCR) is an
escalation path, not a gate. Replaces the current "hold every page 20 s, then fail open" model
(`content.js POLL_MAX_MS`).

## Layered evaluation (in order, first hit wins)

| # | Layer | Where | Latency | Source of truth |
| --- | --- | --- | --- | --- |
| 1 | Device state (unpaired / revoked / paused / monitoring off) | extension, from policy snapshot | 0 ms | snapshot + auth errors |
| 2 | Guardian manual rules (allow/block; device > child > household; block beats allow) | extension | 0 ms | `policy_rules` via snapshot |
| 3 | Schedule (quiet hours) | extension | 0 ms | `child_schedules` via snapshot |
| 4 | Cached high-confidence URL decisions | extension | 0 ms | `url_decision_cache` slice in snapshot |
| 5 | Backend deterministic pass (rules, cache, headline gate) | API/worker | one round trip | Postgres |
| 6 | LangGraph / LLM / OCR escalation | worker, async | seconds | pipeline |

The same rule/schedule evaluation logic runs server-side in the worker (layers 2–3 re-checked
authoritatively), so a stale extension can never *widen* access beyond what the server would
decide — local evaluation is a latency optimization plus an offline floor, not the only check.

## Extension behavior per local outcome

- **Local allow (rule / cached allow / allowlisted):** no loader, page renders. Event still
  posted for history; a later backend decision that is *more restrictive* is applied when it
  arrives.
- **Local block (rule / quiet hours / cached block):** interstitial immediately, no backend
  wait. Event posted with the local decision attached for audit.
- **Unknown:** page renders (no full-screen loader), a small non-blocking "checking" indicator
  shows, event posted, backend decision applied on arrival (SSE fast path, polling fallback).
  Escalation to blur/block replaces the page content at that point. This trades a short
  exposure window on unknown pages for not punishing the safe-page majority; the window is
  bounded by layer-5 latency (target < 1 s for deterministic backend answers).
- **Unknown + high local risk signal** (URL/title matches the snapshot's high-risk token list):
  hold with the loader as today, but capped at 5 s, then **blur + warn** (not allow) until a
  decision arrives.

## Policy snapshot (versioned, product goal #3)

`GET /v1/device/policy` (device-token auth) returns:

```json
{
  "snapshot": {
    "version": "sha256-prefix of canonical content",
    "issued_at": 1730000000000,
    "expires_at": 1730086400000,
    "refresh_after_seconds": 900,
    "household_id": "hh_…", "child_id": "child_…", "device_id": "dev_…",
    "policy_version": "1.0.0",
    "device_status": "active",
    "token_expires_at": 1732592000000,
    "monitoring_enabled": true,
    "monitoring_active": true,
    "paused_until": null,
    "strictness": "standard",
    "rules": [
      {"id": "rule_…", "action": "block", "rule_type": "domain", "pattern": "roblox.com",
       "scope": "child", "expires_at": null}
    ],
    "quiet_hours": [
      {"days": "Mon,Tue", "quiet_start": "21:00", "quiet_end": "07:00",
       "timezone": "Europe/London", "device_id": null}
    ],
    "cached_decisions": [
      {"normalized_url": "https://example.com/x", "action": "block", "reason": "llm:high"}
    ]
  }
}
```

Fetching the snapshot stamps `devices.policy_fetched_at` — that is the device's
policy-freshness signal for the dashboard. The extension refreshes via `chrome.alarms` every
15 min and on service-worker startup; `version` short-circuits no-op refreshes.

### Freshness states (extension side)

| State | Condition | Behavior |
| --- | --- | --- |
| `current` | age < `refresh_after_seconds` | full layered evaluation |
| `stale_usable` | age < `expires_at` | same, refresh attempts continue |
| `expired` | age ≥ `expires_at` | local rules/schedules still enforced; unknown pages get a visible "protection degraded" notice and are posted for later analysis; aggressive refresh retry |
| `unavailable` | paired but never fetched / storage lost | treat as `expired` behavior |
| `device_revoked` | API returns 401 `device_revoked` | clear snapshot + token, show unpaired state in popup, never hold pages |
| `unpaired` | no token | extension inert |

## Failure policy (product goal #5) — risk-tiered, not blanket

Enforcement states beyond plain allow/block: `review_pending` (interim `warn`+`pending_ocr`,
exists today), `offline_policy_applied` (local layers only), `protection_degraded` (expired
snapshot + backend unreachable), `system_uncertain` (LLM invalid output / call failure).

- **LLM call failure / invalid output** → judge returns `action=warn, severity=medium,
  confidence=0.0, categories=[system_uncertain]` (today: `allow`). Low confidence keeps it out
  of the URL cache and triggers OCR escalation where enabled. High-risk headline signals still
  block deterministically regardless of the LLM.
- **Worker outage / queue backlog** → extension shows no loader on unknown pages (see above),
  so nothing hangs; jobs process late; decisions apply late; guardian sees queue-lag on the
  dashboard (device health slice).
- **API outage / offline** → `offline_policy_applied`: layers 1–4 keep enforcing (blocklists,
  quiet hours, cached blocks survive offline); unknown pages render with the degraded notice.
  Low-risk content is never indefinitely blocked by a dead backend; known-bad stays blocked.
- **OCR failure/timeout** → existing `pending_ocr` interim `warn` stands; upgrade re-run without
  OCR text re-judges (never silently allows).

## Backend deterministic pass (layer 5)

`POST /v1/event` already returns synchronously; the worker pipeline's cheap prefix (manual
rules → pause/quiet gates → URL cache → headline) needs no LLM and typically answers in tens of
milliseconds. Manual rules are evaluated in the worker before the URL cache so a guardian rule
always beats a stale cached LLM decision.

## Exact-match domain rules (fixes substring matching)

All domain matching (policy engine allow/block sets, headline tokens, manual rules) moves to
suffix matching: `host == pattern` or `host.endswith("." + pattern)`. `"bet"`-in-`alphabet.com`
class of false positives dies; `.edu`-in-`*.education` class of false allows dies.
