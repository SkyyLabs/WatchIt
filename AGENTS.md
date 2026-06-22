# WatchIt Agent Guide

This file is the persistent project guide for new Codex sessions. Read it before making changes.

## 1. Project Overview

WatchIt is a parental web-monitoring product. It combines a Chromium browser extension, a FastAPI event API, background AI safety workers, a Clerk-authenticated guardian dashboard, and Neon/Postgres storage.

Primary users:
- Guardians/parents who review browsing activity, configure child profiles, pause/resume monitoring, and override decisions.
- Children whose browser activity is monitored through the browser extension.
- Future operators/developers who deploy the API, dashboard, worker, and database services.

Product goals:
- Detect unsafe or age-inappropriate web activity.
- Enforce page-level actions in the browser extension: allow, warn, blur, block, or notify.
- Provide guardians with a dashboard for live decisions, history, child settings, extension pairing, and monitoring controls.
- Use asynchronous processing so event ingestion stays fast while AI/OCR work runs in workers.
- Keep the architecture cloud-ready while allowing local LLM/Ollama fallback through environment variables.

Current MVP scope:
- Browser-extension-based monitoring for v1.
- Clerk authentication for dashboard guardians.
- Household-scoped data model with guardians, children, devices, monitoring sessions, events, decisions, overrides, schedules, screenshot metadata, and audit logs.
- Postgres/Neon as the source of truth, managed through Alembic migrations.
- Agent worker pipeline with URL safety, headline checks, Docling OCR/ASR hooks, LLM judging, policy decisions, URL decision cache, and queue timing instrumentation.
- Local screenshot files only under `screenshots/YYYY-MM-DD/{event_id}/`; database stores metadata only.

Major assumptions and constraints:
- Desktop/macOS agent mode is deferred.
- RAG and billing are not implemented yet.
- `pgvector` is enabled but there are no RAG/vector tables yet.
- Screenshots must not be stored in Neon as bytes.
- Browser extension event ingest must use device tokens, not Clerk guardian tokens.
- Production auth depends on correct `CLERK_ISSUER`/`CLERK_JWKS_URL` matching the dashboard Clerk instance.

## 2. Project Architecture

Frontend:
- `apps/dashboard` is a Next.js App Router app using React.
- Routes live under `apps/dashboard/src/app`.
- `src/proxy.ts` uses Clerk middleware.
- The dashboard is currently mostly client-rendered from `page.tsx`; server logging is in `src/lib/server-logger.ts`, client logging is in `src/lib/client-logger.ts`.

Backend/API:
- `apps/api/src/watchit_api/main.py` is the FastAPI application.
- API concerns include event ingest, device pairing, monitoring sessions, child settings, guardian decisions, SSE streams, pause/resume controls, and lifecycle startup.
- `apps/api/src/watchit_api/auth.py` owns Clerk guardian verification and device token auth.
- `apps/api/src/watchit_api/schemas.py` contains request models.
- `apps/api/src/watchit_api/sse.py` streams decisions and must preserve household filtering.

Database/storage:
- Postgres/Neon is the primary datastore.
- Alembic is the migration owner. Add schema changes under `migrations/versions`.
- Current production table names do not use a `watchit_` prefix.
- `packages/core/src/watchit_core/db.py` is the central repository layer. Prefer adding repository methods there instead of scattering raw SQL through API/worker modules.
- Local screenshots are saved through `packages/core/src/watchit_core/screenshot_store.py`.
- `.env` supplies `DATABASE_URL`; do not print or commit secrets.

Authentication/authorization:
- Guardian dashboard auth uses Clerk bearer tokens.
- API guardian dependencies resolve Clerk claims into `guardians`, `households`, and `household_members`.
- The first authenticated guardian to hit the API can claim the legacy household if it has no members.
- Browser extension auth uses paired device tokens. Device tokens map to `devices` and derive `household_id`, `child_id`, and `device_id`.
- Every guardian-facing query must be scoped to the authenticated household.
- Every extension event must be scoped from the authenticated device token, not from client-submitted child or household ids.

AI/agentic components:
- `services/agent-worker` runs the LangGraph safety pipeline.
- `watchit_agents/runtime.py` coordinates event processing, pause checks, screenshot persistence, URL cache lookup/write, graph invocation, decision write, SSE publish, and pipeline timing logs.
- `watchit_agents/llm_provider.py` selects Anthropic, OpenAI-compatible cloud, or Ollama providers from env vars.
- `watchit_agents/llm_judge.py`, `safety.py`, `graph.py`, and `agents/*` implement classification, planning, Docling OCR, headline, URL, and policy steps.
- RAG is not present yet. Do not add vector/RAG tables or dependencies unless explicitly requested.

Browser extension and workers:
- `apps/browser-extension` is a Chromium MV3 extension.
- `background.js` observes navigation, collects DOM samples, handles screenshots, redeems pairing codes, stores device tokens, posts events, and listens for SSE decisions.
- `content.js` applies page-level allow/warn/blur/block behavior.
- `popup.html`/`popup.js` support device pairing.
- `services/agent-worker` can run embedded in the API for local development or as a standalone worker.
- `services/learning-worker` processes guardian overrides and stores learned feedback.

Local-first vs cloud/shared behavior:
- Local development can run API, embedded worker, dashboard, extension, Ollama, and Postgres/Neon from one machine.
- Production direction is Vercel dashboard, Render or similar long-running worker/API host, Neon Postgres, Clerk auth, and Claude primary with local Ollama fallback only where explicitly configured.

Privacy/security-sensitive decisions:
- No screenshot bytes in Neon.
- Avoid logging secrets, bearer tokens, device tokens, pairing codes after creation, or raw screenshots.
- Keep household isolation strict for all reads, writes, SSE streams, caches, overrides, and settings.
- Device pairing codes are short-lived and one-time use.
- Store token hashes, not raw device tokens.

## 3. Core Features

User-facing features:
- Browser extension captures web visits, DOM samples, and optional screenshots for OCR upgrades.
- Extension applies decisions to pages: allow, warn, blur, block, notify.
- Extension popup pairs the browser install with a child profile using a dashboard-generated pairing code.

Guardian dashboard features:
- Clerk sign-in/sign-out.
- Recent decision feed with action, reason, title, URL, and manual override control.
- Child profile creation and settings for age and strictness.
- Pairing code generation for the selected child.
- Start/stop monitoring sessions for the selected child.
- Pause/resume monitoring with parent PIN.
- Client log export.

AI/automation features:
- Asynchronous event queue and worker pipeline.
- Fast safety checks, headline agent, URL/LLM judge, Docling OCR path, policy engine, URL decision cache, and guardian learning from overrides.
- Structured timing logs for queue wait, pipeline steps, cache behavior, and decision publishing.

Data collection, storage, and reporting:
- Events store URL, normalized URL, domain, title, referrer, tab id, raw JSON, child/device/session/household scope, and timestamps.
- Analysis rows store model/version outputs, labels, scores, and latency.
- Decisions store policy action/reason/details and original action.
- Overrides are separate rows so original decisions remain auditable.
- Screenshot metadata is stored in `screenshot_files`; screenshot image files stay local.

Incomplete or planned:
- Desktop/macOS local agent mode.
- RAG/pgvector document retrieval.
- Billing/subscriptions.
- Production deployment automation and log drains.
- Full test suite. Current verification is mostly compile/build/manual API checks.

## 4. Folder Structure

Root:
- Belongs: repo-level docs, `AGENTS.md`, `README.md`, `Makefile`, package manifests, Alembic config, migrations, environment examples.
- Does not belong: secrets, screenshots, logs, local DB files, build artifacts, generated `.next` files.
- Naming: use concise lowercase files for scripts/config; use conventional migration ids under `migrations/versions`.

`apps/api`:
- Belongs: FastAPI app, route handlers, auth dependencies, request schemas, SSE helpers.
- Does not belong: database schema DDL, worker graph logic, dashboard UI, extension code.
- Naming: Python modules use snake_case; API payload classes use PascalCase.
- Imports: import shared config/repository/logging from `watchit_core`; import worker runtime only where API intentionally embeds or triggers processing.

`apps/dashboard`:
- Belongs: Next.js App Router UI, Clerk pages/middleware, dashboard-specific client/server logging.
- Does not belong: raw Postgres access, Python API logic, extension service worker code.
- Naming: React components use PascalCase when extracted; hooks/helpers use camelCase; route folders follow Next.js conventions.
- Imports: prefer relative imports within dashboard for UI helpers; do not import backend Python concepts.

`apps/browser-extension`:
- Belongs: MV3 manifest, background service worker, content script, popup UI, extension-only JS.
- Does not belong: React dashboard code, server secrets, Clerk secret keys.
- Naming: plain browser JS; keep message types explicit, e.g. `watchit_pair`, `watchit_decision`.
- Imports: keep extension dependency-free unless a dependency is clearly justified and bundling is configured.

`services/agent-worker`:
- Belongs: queue consumer, LangGraph workflow, safety/LLM/OCR agents, event pipeline runtime.
- Does not belong: FastAPI route declarations, dashboard UI, schema migrations.
- Naming: Python modules use snake_case; agent classes use PascalCase.
- Imports: shared DB/config/logging from `watchit_core`; app-specific graph code from `watchit_agents`.

`services/learning-worker`:
- Belongs: guardian override learning loop and feedback synthesis.
- Does not belong: main event pipeline, dashboard UI, schema DDL.
- Naming/imports: same Python conventions as other services.

`packages/core`:
- Belongs: shared config, DB repository, migrations runner, logging, policy engine, URL cache helpers, screenshot storage, shared types.
- Does not belong: UI code, FastAPI route declarations, browser extension scripts.
- Naming: modules use snake_case; repository methods should be explicit about scope, e.g. `household_id`, `device_id`, `child_id`.
- Imports: keep core mostly independent; avoid importing API/dashboard modules into core.

`migrations`:
- Belongs: Alembic env, templates, ordered version files.
- Does not belong: ad hoc SQL scripts not tracked by Alembic.
- Naming: `YYYYMMDD_NNNN_short_description.py`.
- Imports: migrations should be deterministic and avoid app runtime imports except Alembic/SQLAlchemy.

`logs`, `screenshots`, `.docling_cache`, `.venv`, `node_modules`, `.next`:
- Local/generated only. Do not commit.

## 5. Coding Practices

TypeScript/JavaScript:
- Use TypeScript for dashboard code. Existing extension code is plain JavaScript; keep it simple and dependency-free unless bundling is added.
- Prefer `const`/`let`, explicit async error handling, and clear object shapes.
- Avoid leaking tokens or sensitive payloads into client logs.

React/Next.js:
- Keep route-level UI in `src/app`; extract reusable UI only when duplication is real.
- Client components must start with `"use client"`.
- Server-only code, secrets, and Pino server logging stay out of client components.
- Do not add a landing page when the requested work is app functionality; the dashboard should remain the primary experience.
- Keep forms accessible: labels, disabled states, clear errors, keyboard-friendly controls.

Server/client boundaries:
- Browser and dashboard clients call FastAPI over HTTP.
- Do not connect the dashboard directly to Postgres.
- Do not expose Clerk secret keys, database URLs, Anthropic/OpenAI keys, or device token hashes to the browser.

API route design:
- Use Pydantic models for request bodies.
- Guardian endpoints must depend on `require_guardian`.
- Extension ingest endpoints must depend on `require_device`.
- Scope every query by authenticated household/device context.
- Raise appropriate `HTTPException` values instead of returning ambiguous errors.

State management:
- Dashboard currently uses local React state. Keep state local until shared complexity justifies a library.
- Persist only safe client state in `localStorage`; do not persist raw Clerk tokens manually.
- Extension stores device token and install metadata in `chrome.storage.local`.

Error handling and validation:
- Validate age, actions, pairing codes, monitoring state, and required ids at API boundaries.
- Treat missing or invalid auth as 401, forbidden scope as 403, missing resources as 404, inactive monitoring as 409.
- Avoid broad `except` blocks unless logging and safe fallback are intentional.

Logging:
- Python services use `structlog` through `watchit_core.logging`.
- Dashboard server logs use Pino.
- Dashboard client logs use `client-logger`.
- Include request id, household id, child id, device id, event id, job id, and timings where available.
- Never log secrets, bearer tokens, raw screenshots, or full sensitive payloads.

Security and privacy:
- Store device token hashes only.
- Keep screenshot bytes local under `screenshots/`; database stores metadata only.
- Preserve household isolation for data reads, writes, SSE, cache, overrides, settings, and logs.
- Pairing codes should be short-lived and one-time use.
- Use environment variables for provider keys and DB URLs.

Accessibility:
- Dashboard controls should have visible labels or obvious button text.
- Maintain keyboard-operable forms and controls.
- Do not rely on color alone for critical state.

Testing:
- Run focused checks for touched areas.
- Python compile check:
  `PYTHONPATH=apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src .venv/bin/python -m compileall apps/api/src services/agent-worker/src services/learning-worker/src packages/core/src migrations`
- Dependency check: `.venv/bin/pip check`
- Dashboard build: `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_dummy CLERK_SECRET_KEY=sk_test_dummy npm run build` from `apps/dashboard`.
- Alembic check: `PYTHONPATH=apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src .venv/bin/alembic heads`.
- Live DB migrations require explicit care; do not run against Neon casually.

Performance:
- Keep event ingestion fast; queue heavy work.
- Preserve URL decision caching behavior.
- Avoid blocking the API on OCR or LLM work unless `WATCHIT_PROCESSING_MODE=sync` is intentionally used.
- Index new query paths by household, child, device, status, and time.

Reusable vs feature-specific code:
- Put cross-service Python helpers in `packages/core`.
- Keep one-off dashboard UI inside the page/component that uses it.
- Extract components/helpers only when they are reused or reduce meaningful complexity.

## 6. AI/Codex Working Rules

- Read `AGENTS.md` before making changes.
- Inspect the current codebase before editing.
- Do not make large rewrites unless explicitly requested.
- Prefer small, safe, reviewable changes.
- Preserve existing behavior unless asked to change it.
- Update related tests, types, migrations, and documentation when needed.
- Do not introduce new dependencies without a clear reason.
- Do not remove files unless they are clearly unused or explicitly requested.
- Do not revert user changes or unrelated dirty work.
- Use Alembic for schema changes.
- Keep secrets out of command output, logs, docs, and commits.
- When unsure, leave a short note in the final response rather than guessing silently.

## 7. Git Commit Message Guidelines

Use conventional commit style.

Examples:

```txt
feat: add parent dashboard summary cards
fix: resolve child profile form validation bug
refactor: move shared date helpers into utils
docs: add project architecture guide
test: add coverage for safety analyzer
chore: clean up unused assets
style: improve dashboard spacing
```

Rules:
- Use lowercase commit types.
- Keep the first line under 72 characters.
- Use imperative mood.
- Mention the affected module when helpful.
- Include a short body when the change needs explanation.

Allowed types:
- `feat`
- `fix`
- `refactor`
- `docs`
- `test`
- `chore`
- `style`
- `perf`
- `build`
- `ci`

## 8. Pull Request Message Guidelines

Use this PR template:

```md
## Summary
- 

## Changes
- 

## Testing
- 

## Screenshots / Demos
- 

## Risks
- 

## Notes for Reviewer
- 
```

Rules:
- The summary should explain the user-facing or developer-facing impact.
- The changes section should list meaningful implementation details.
- The testing section should include exact commands run.
- The risks section should mention migrations, auth changes, data handling, or UI behavior changes.
- Add screenshots for UI changes whenever possible.
- Mention follow-up work separately instead of hiding it in the summary.

## 9. Documentation Maintenance

Update this file whenever:
- Architecture changes.
- Folder structure changes.
- New major features are added.
- Coding conventions change.
- Build, test, or deployment commands change.

If something is unknown, write `Unknown / not yet documented` instead of guessing.
