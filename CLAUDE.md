# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

WatchIt: cloud-ready parental web-monitoring. A Chromium extension, a FastAPI event API, background AI safety workers, a Clerk-authenticated guardian dashboard, and Neon/Postgres storage. Postgres is the source of truth. Events process asynchronously so ingest stays fast while AI/OCR runs in a worker. Claude is the production LLM; local Ollama is the fallback.

## Working in this repo

Bias toward caution over speed. For trivial tasks, use judgment.

**Think before coding.** State assumptions explicitly; if uncertain, ask. When multiple interpretations exist, present them — do not pick silently. When something is unclear, stop and name it. Several invariants below (auth scoping, no screenshot bytes in DB, Alembic-only schema) are load-bearing and easy to break silently — confirm rather than guess.

**Don't over-engineer, over-modularize, or duplicate.** Minimum code that solves the problem; abstract only on proven reuse; share what's genuinely shared. Full rules and the anti-patterns in `.claude/rules/coding-practices.md` — read it before writing code.

**Goal-driven execution.** Turn tasks into verifiable goals ("add validation" → cover the invalid inputs and confirm they're rejected). State a brief plan for multi-step work and loop until each step is verified with real output (`make verify`), not assumed.

## Commands

Every Python command needs `PYTHONPATH` set to the four `src` roots. The Makefile exports it as `apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src`. Prefer `make` targets; use raw commands only where no target exists.

```bash
make setup             # venv + deps + ollama + pull-model (full local bootstrap)
make run-api           # uvicorn watchit_api.main:app on 127.0.0.1:4849 (embedded worker by default)
make run-agent-worker  # standalone worker: python -m watchit_agents.worker
make run-dashboard     # next dev in apps/dashboard
make db-init           # alembic upgrade head against DATABASE_URL
```

Run the worker out-of-process: start the API with `WATCHIT_EMBEDDED_AGENT_WORKER=false make run-api`, then `make run-agent-worker` in another shell.

### Verification

```bash
make verify   # compile + pip check + alembic heads + pytest + dashboard typecheck
```

`make verify` is the single source of truth for the check sequence (also run in CI). A thin pytest suite lives in `tests/`; add cases there when you touch pure logic (policy, url_cache, auth scoping). For a full dashboard production build use dummy Clerk keys — see `.claude/rules/frontend.md`. Never run migrations against Neon casually — live DB changes need explicit care.

## Architecture

Five deployables, one Postgres source of truth. Event flow is **async by design** — ingest stays fast, AI/OCR runs in a worker.

```
apps/browser-extension  Chromium MV3: capture visits/DOM, apply page actions, device pairing
apps/api                FastAPI: ingest, controls, SSE, Clerk-guarded guardian endpoints
apps/dashboard          Next.js App Router + Clerk: guardian UI (mostly client-rendered)
services/agent-worker   LangGraph safety pipeline (embeddable in API for local dev)
services/learning-worker  Guardian-override learning loop
packages/core           Shared config, DB repository, policy engine, url cache, screenshot store
```

### Request flow (load-bearing path)

1. Extension `POST /v1/event` → API writes a queued job to Postgres, returns `status=queued` immediately.
2. `services/agent-worker` claims queued jobs and runs the pipeline in `watchit_agents/runtime.py`: pause check → profile load → URL decision-cache lookup → LangGraph (`graph.py`: deterministic headline→url_llm→ocr→policy routing, no LLM planner) → analysis + decision writes → SSE publish. Emits per-step timing logs.
3. On OCR need, the extension screenshots and `POST /v1/event/upgrade` re-enqueues with image data.
4. Dashboard reads history from Postgres and streams live decisions via `GET /v1/stream/decisions` (SSE) after Clerk sign-in.

The queue is Postgres-backed (`packages/core/src/watchit_core/services/queue.py`), not an external broker — no Redis/RabbitMQ.

### Key modules

- `apps/api/src/watchit_api/`: `main.py` (FastAPI app + startup), `auth.py` (guardian + device auth), `schemas.py` (Pydantic request models), `sse.py` (decision stream).
- `services/agent-worker/src/watchit_agents/`: `runtime.py` (pipeline orchestration), `graph.py` + `agents/*` (headlines, url, ocr, policy), `llm_provider.py` (provider selection), `llm_judge.py`, `safety.py`, `ocr_asr.py` (Docling), `worker.py` (queue consumer).
- `packages/core/src/watchit_core/`: `db.py` (repository), `config.py`, `policy/engine.py`, `url_cache.py`, `screenshot_store.py`, `services/queue.py`, `migrations.py`, `logging.py`.
- `apps/dashboard/src/`: routes under `app/`, UI in `components/` (shadcn/ui primitives under `components/ui`), HTTP client in `lib/api-client.ts`, Clerk middleware in `proxy.ts`.

## Specialist rules (`.claude/rules/`)

Domain constraints live in focused files. **Read the relevant one before editing that area** — they carry detail this file summarizes.

| File | Read when touching |
| --- | --- |
| `.claude/rules/coding-practices.md` | any code change — over-engineering / modularization / duplication rules |
| `.claude/rules/database.md` | `db.py`, `migrations/`, any SQL/schema/Alembic work |
| `.claude/rules/api.md` | `apps/api/` — routes, auth, schemas, SSE |
| `.claude/rules/frontend.md` | `apps/dashboard/` — Next.js UI, Clerk, client/server boundary |
| `.claude/rules/agent-worker.md` | `services/agent-worker/`, `services/learning-worker/` — pipeline, LLM, OCR, queue |
| `.claude/rules/extension.md` | `apps/browser-extension/` — MV3 background/content/popup |
| `.claude/rules/security.md` | anything auth, storage, logging, or data handling (cross-cutting) |

## Critical invariants (never break)

Full detail in the rule files; the load-bearing ones, always in force:

- **Two auth models, never mixed** (`auth.py`): guardian routes → `require_guardian` (Clerk), extension ingest → `require_device` (device token). Scope every query by authenticated context, never by client-submitted ids. See `api.md` / `security.md`.
- **Alembic-only schema**, single head, no `watchit_` prefix; `db.py` is the single repository layer. See `database.md`.
- **No screenshot bytes in Postgres** — files local, metadata only. **Store hashes, not secrets** (device tokens, parent PIN). **Overrides are new rows.** See `security.md`.
- **Dashboard never touches Postgres** — HTTP API only. **Extension is dependency-free plain JS.** See `frontend.md` / `extension.md`.
- **Never log** secrets, tokens, pairing codes, or raw screenshots.

## Conventions

- **Shared Python** goes in `packages/core`; never import API/dashboard modules into `core`. Extract only on real reuse.
- **Logging:** Python `structlog` via `watchit_core.logging`; dashboard server Pino, client `client-logger`. Include request/household/child/device/event/job ids and timings.
- **Scope discipline:** small reviewable changes, preserve existing behavior unless asked, no new deps without clear reason, remove no files unless clearly unused or requested.
- **Commits:** conventional, lowercase type, imperative, first line < 72 chars. Types: `feat` `fix` `refactor` `docs` `test` `chore` `style` `perf` `build` `ci`.
- **PRs:** Summary, Changes, Testing (exact commands), Screenshots/Demos, Risks (migrations, auth, data, UI), Notes for Reviewer.

## Config

Backend config is env-driven via `.env` at repo root (`DATABASE_URL`, `CLERK_ISSUER`/`CLERK_JWKS_URL`, LLM keys, `WATCHIT_*` flags). Dashboard uses `apps/dashboard/.env.local` (`NEXT_PUBLIC_CLERK_*`). The README config table lists every `WATCHIT_*` flag and default. Key flags: `WATCHIT_PROCESSING_MODE` (async/sync), `WATCHIT_EMBEDDED_AGENT_WORKER`, `WATCHIT_LLM_PROVIDER`, `WATCHIT_ENABLE_OCR`, `WATCHIT_URL_DECISION_CACHE_*`.
