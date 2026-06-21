# WatchIt Architecture Review

## Architecture Decision

WatchIt is now structured as a deployable monorepo with separate apps, workers, and shared packages. This is the right midpoint before splitting repositories: the boundaries are real, but coordinated product changes still happen in one place.

## Target System

- `apps/api`: FastAPI boundary for event intake, dashboard reads, controls, child settings, overrides, and SSE.
- `apps/dashboard`: Next.js guardian dashboard with Clerk authentication.
- `apps/browser-extension`: Chromium extension for page capture and browser enforcement.
- `services/agent-worker`: asynchronous event processor for LangGraph planning, OCR, LLM judgment, policy execution, and decision writes.
- `services/learning-worker`: guardian feedback loop that distills manual overrides into prompt guidance.
- `packages/core`: shared config, Neon/Postgres repository, policy engine, screenshot storage, logging, and common types.

There is intentionally no sync worker. Postgres is the primary datastore, not a mirror.

## Runtime Flow

1. The extension posts `/v1/event` with visit metadata and DOM text.
2. The API validates the payload, writes an event job into Postgres, and returns immediately with `status=queued`.
3. The agent worker claims jobs with `FOR UPDATE SKIP LOCKED`, runs the safety pipeline, persists analyses/decisions, and publishes decisions over SSE.
4. If a decision requests OCR, the extension captures a screenshot and posts `/v1/event/upgrade`.
5. Upgrade jobs run OCR first, then rerun LLM/policy evaluation and publish the final decision.

This removes OCR and model latency from the HTTP request lifecycle. Local development can keep `WATCHIT_EMBEDDED_AGENT_WORKER=true`; production should run the API and worker as separate processes.

## Worker Host

A worker host is the platform that runs long-lived background processes outside the web request path. For WatchIt, the selected host is Render:

- Vercel hosts the dashboard and can host the API if desired.
- Render hosts `services/agent-worker` as a background worker.
- Both API and worker connect to the same Neon `DATABASE_URL`.
- The worker has model/OCR dependencies and can run longer tasks without tying up web functions.

## Data Architecture

Neon/Postgres is the source of truth. `packages/core/src/watchit_core/db.py` owns schema creation and repository access for:

- children
- events
- analyses
- decisions
- settings
- event jobs

The database initializes `pgvector` with `CREATE EXTENSION IF NOT EXISTS vector` so future RAG storage can be added without changing providers. SQLCipher was useful for an earlier local-first prototype, but it does not fit the eventual cloud architecture and has been removed from the runtime path.

## Auth Architecture

Clerk is the guardian identity provider.

- The dashboard uses `@clerk/nextjs`, `ClerkProvider`, Clerk sign-in/sign-up routes, and Clerk bearer tokens.
- The API verifies Clerk JWTs on guardian/admin endpoints using Clerk JWKS.
- Extension ingest endpoints are intentionally separate from guardian auth. Before production, they need a dedicated device credential or device enrollment token so browser extensions can post events without pretending to be guardian users.

## LLM Provider Strategy

`watchit_agents.llm_provider.build_chat_model` centralizes model selection.

- `WATCHIT_LLM_PROVIDER=anthropic`: Claude primary, with local Ollama fallback.
- `WATCHIT_LLM_PROVIDER=ollama`: local Llama/Ollama-only mode for development.
- `WATCHIT_LLM_PROVIDER=openai`: optional cloud provider path retained for experimentation.

The intended production default is Claude plus local fallback where a local worker has Ollama available.

## Remaining Technical Work

- Add a device-token model for extension ingest.
- Replace the in-memory SSE bus when there are multiple API instances.
- Add migrations instead of repository-managed schema creation before production.
- Modularize `apps/dashboard/src/app/page.tsx` into focused dashboard modules once behavior is covered.
- Add tests for queue claim/complete/fail, OCR upgrade flow, policy outputs, Clerk auth, overrides, child settings, and SSE payload shape.

## Final Structure

```text
apps/
  api/
  dashboard/
  browser-extension/
services/
  agent-worker/
  learning-worker/
packages/
  core/
```

This gives WatchIt clean production boundaries now while preserving the option to split services into separate repositories later.
