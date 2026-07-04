# Security & privacy rules

Applies to all code — auth, storage, logging, and data handling. Cross-cutting; read alongside the domain rule for the file you're touching.

## Household isolation

Every read, write, SSE stream, cache lookup, override, and setting is scoped to the authenticated household (guardian path) or device-derived household (extension path). Never trust client-submitted `household_id` / `child_id`. This is the core privacy guarantee — breaking it leaks one family's data to another.

## Store hashes, not secrets

- Device tokens: store the hash, never the raw token.
- Parent PIN: store the hash, never plaintext. Set only through the dashboard onboarding/settings UI — never via `.env`.
- Pairing codes: short-lived, one-time use.

## Screenshots

Image bytes never enter Postgres. Files stay local under `screenshots/YYYY-MM-DD/{event_id}/`; the DB holds metadata only (`screenshot_files`).

## Logging

Never log: secrets, bearer tokens, device tokens, pairing codes (after creation), raw screenshots, or full sensitive payloads. Do log safe context: request/household/child/device/event/job ids and timings.

## Secrets

Provider keys (Anthropic/OpenAI), `DATABASE_URL`, and Clerk keys come from environment variables. Keep them out of command output, client bundles, logs, docs, and commits.

## Destructive-command guard

A PreToolUse hook (`.claude/hooks/guard-destructive.py`, wired in `.claude/settings.json`) intercepts every Bash command and forces a confirm (`ask`) on destructive patterns: recursive `rm`, deleting `child_monitor.db`, `git push --force`, `git reset --hard`, `git clean`, `alembic downgrade`, `dropdb`, SQL `DROP`/`TRUNCATE`/`DELETE`, overwriting `.env`. `alembic downgrade` is additionally hard-denied in settings. This is a safety net, not a sandbox — `ask` still lets you approve intentional destructive work; it just never runs silently.
