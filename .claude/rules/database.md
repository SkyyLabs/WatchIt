# Database rules

Applies when touching `packages/core/src/watchit_core/db.py`, `migrations/`, or any SQL/schema work.

- **Postgres/Neon is authoritative.** No other datastore is a source of truth.
- **`db.py` is the single repository layer.** Add query methods there. Never scatter raw SQL through API or worker modules. Repository methods name their scope explicitly (`household_id`, `child_id`, `device_id`).
- **Alembic owns schema.** All schema changes go under `migrations/versions`, named `YYYYMMDD_NNNN_short_description.py`. Never hand-edit tables or write ad-hoc DDL outside Alembic. API and workers run migrations on startup — keep a single Alembic head (`alembic heads` must show one).
- **Production tables have no `watchit_` prefix.**
- **Everything is household-scoped.** Every read and write filters by the authenticated household (or device-derived household for extension paths). Index new query paths by household/child/device/status/time.
- **`pgvector` is enabled but unused** — no RAG/vector tables exist. Do not add RAG tables or deps unless explicitly asked.
- **Screenshots store metadata only** (`screenshot_files` table). Never store image bytes in Postgres — files stay local under `screenshots/YYYY-MM-DD/{event_id}/`.
- Data model: guardians, children, devices, monitoring sessions, events, decisions, overrides, schedules, screenshot metadata, audit logs.
- **Overrides are new rows**, never mutations of the original decision — keeps decisions auditable.
- Never run migrations against Neon casually. Live DB changes need explicit care.

## Tables by area

- **Identity/tenancy:** `guardians`, `households`, `household_members`
- **Children/devices:** `children`, `devices`, `device_pairing_codes`, `monitoring_sessions`
- **Activity/AI:** `events`, `event_jobs`, `analysis`, `decisions`, `decision_overrides`, `url_decision_cache`
- **Settings/safety/audit:** `child_schedules`, `household_settings`, `guardian_feedback`, `screenshot_files`, `audit_log`, `policy_versions`
- **Legacy:** `settings_legacy`

## Key indexes (mirror these when adding query paths)

`children(household_id)` · `devices(household_id, child_id)` · `monitoring_sessions(household_id, child_id, device_id, status)` · `events(household_id, child_id, ts DESC)` · `events(device_id, ts DESC)` · `events(household_id, domain)` · `event_jobs(status, created_at)` · `analysis(household_id, event_id)` · `decisions(household_id, event_id)` · `decision_overrides(household_id, decision_id)` · `url_decision_cache(household_id, child_id, normalized_url)` · `url_decision_cache(expires_at)` · `screenshot_files(event_id)` · `audit_log(household_id, created_at DESC)`

## Queue claim

`event_jobs` is claimed with `FOR UPDATE SKIP LOCKED` so multiple workers never grab the same job. Preserve that clause on any new queue query. Index `event_jobs(status, created_at)` backs the claim.

## Parent PIN storage

Stored in `household_settings` under key `parent_pin` as a hash record — never plaintext:

```json
{ "algorithm": "pbkdf2_sha256", "iterations": 260000, "salt": "...", "hash": "...", "updated_at": "..." }
```

PIN rules: 4–8 numeric digits; changing an existing PIN requires the current PIN; first-time setup happens in dashboard onboarding/settings, not `.env`.

## History note

SQLCipher (used by an earlier local-first prototype) was removed — Postgres/Neon is the only datastore. Don't reintroduce local encrypted-DB paths.
