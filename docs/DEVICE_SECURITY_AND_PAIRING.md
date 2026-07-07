# Device Security & Pairing

The existing flow (guardian-minted 6-digit code → unauthenticated redeem → opaque device token,
hash-stored, install-id upsert) is a sound foundation. This hardens it; it does not rebuild it.

## Threat model (what we defend against)

1. Attacker without a code brute-forcing `/v1/device/redeem` into someone's household.
2. Stolen device token (exfiltrated `chrome.storage.local`) used from another machine, forever.
3. Revoked/compromised device silently continuing to browse unprotected.
4. Paired device in household A reading or writing household B's data (cross-tenant).
5. Sibling device reading another child's decision stream inside the same household.

Out of scope: a child with OS admin rights removing the extension entirely (that is a device
-management problem; we surface it via device health, we cannot prevent it from a browser
extension).

## Pairing codes

- 6 digits, SHA-256 stored, TTL 15 min, atomic single-use — **kept**.
- **Added: redemption throttling.** Per-IP sliding-window limit and a global budget on
  `/v1/device/redeem` (429 beyond limits), plus a structured `device_redeem_failed` /
  `device_redeem_throttled` log line per failure (`audit_log` needs a household id, which a
  failed redemption doesn't have). With
  ≤10 attempts/min/IP, sweeping the 10^6 space within a 15-minute TTL is not practical.
  Limiter is in-process (single API instance today); moves to Redis/Postgres when the API
  scales out.
- Codes remain one-per-child mint with guardian audit (`device_pairing_code_created`).

## Device tokens

- Opaque `wdev_` + 32-byte urlsafe secret, SHA-256 hash in `devices.token_hash` — **kept**.
- **Added: expiry.** `devices.token_expires_at` (30 days from issue).
  `authenticate_device_token` rejects expired tokens with a distinct `token_expired` error so
  the extension can distinguish "rotate" from "revoked".
- **Added: rotation.** `POST /v1/device/token/rotate` (device auth) issues a new token and a new
  expiry. The previous hash stays valid for a 1-hour grace window
  (`token_prev_hash`/`token_rotated_at`) so a lost rotation response cannot brick the device.
  The extension rotates when the snapshot's `token_expires_at` is < 7 days away.
- Re-pairing (same `install_id`) still rotates the token and reactivates the device.

## Revocation & compromised-device recovery

- `DELETE /v1/devices/{id}` sets `status='revoked'` — kept. Auth now returns a structured 401
  body `{"code": "device_revoked"}`; the extension reacts by clearing its token + snapshot and
  showing "unpaired" in the popup, and stops holding/marking pages (a revoked device is
  *unmonitored*, and the dashboard shows it as such — revocation is not a block).
- Recovery playbook for a suspected-compromised token: revoke device (kills token instantly),
  re-pair with a fresh code (rotates everything). Guardian-visible: device drops from the
  active list; audit trail `device_revoked` → `device_paired`.

## Device health & policy freshness

- `devices.last_seen_at` is already bumped on every authenticated call.
- **Added:** `devices.policy_fetched_at`, stamped whenever the device pulls
  `GET /v1/device/policy`. Dashboard derives: **healthy** (policy fetched < 1 h), **stale**
  (seen recently, policy old — extension broken or blocked), **offline** (not seen > 24 h),
  **paused**, **revoked**.

## Authorization boundaries (fixes)

- **Upgrade scoping (critical fix):** `POST /v1/event/upgrade` verifies the target event
  belongs to the authenticated device's household before enqueueing. Previously any paired
  device could rewrite any household's event payloads by id.
- **Device SSE stream:** filtered by `device_id`, not just household — a device only receives
  decisions addressed to it (no sibling browsing leakage).
- Guardian streams stay household-filtered with per-request membership validation (existing).
- SSE device-token-in-query-string is acknowledged tech debt: the durable credential appears in
  URLs. Planned replacement: short-lived stream tickets minted via authenticated POST. Until
  then the polling path (Authorization header) is the primary decision channel and SSE is an
  optimization.

## Reassignment semantics (unchanged, documented)

Redeeming a code for child B on a device paired to child A moves the device (install-id upsert),
stops A's device-scoped session, and writes `device_reassigned` audit rows to both households.
Historical events stay with the original household.

## Audit trail

Existing: `device_paired`, `device_reassigned`, `device_revoked`, `device_paused`,
`device_resumed`, `device_pairing_code_created`. Added: `device_token_rotated` (audit row) and
structured logs for failed/throttled redemptions (never carrying the attempted code).
