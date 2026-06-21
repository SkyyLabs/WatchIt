# WatchIt

WatchIt is a cloud-ready parental monitoring system with a browser extension, FastAPI event API, asynchronous agent worker, Clerk guardian dashboard, and Neon/Postgres datastore.

The current architecture is optimized for the eventual production shape: Postgres is the source of truth, events are processed asynchronously, Claude can be the primary model, and local Ollama/Llama remains available through environment variables.

## Structure

```text
apps/
  api/                FastAPI event API, controls, SSE, Clerk-protected guardian endpoints
  dashboard/          Next.js guardian dashboard with Clerk auth
  browser-extension/  Chromium extension for page capture and enforcement
services/
  agent-worker/       Background worker for LangGraph, OCR, LLM, and policy decisions
  learning-worker/    Guardian override learning loop
packages/
  core/               Shared config, Postgres repository, policy, logging, screenshots
```

## Runtime Flow

1. The browser extension posts a visit event to `POST /v1/event`.
2. The API writes a durable job to Postgres and returns `status=queued`.
3. The agent worker claims queued jobs, runs the LangGraph safety pipeline, stores analyses and decisions, and publishes decisions over SSE.
4. If OCR is needed, the extension captures a screenshot and posts `POST /v1/event/upgrade`.
5. The dashboard reads historical events/decisions from Postgres and streams live decisions after Clerk sign-in.

Local development can run the worker embedded in the API. Production should run the API and worker separately.

## Prerequisites

- Python 3.11
- Node.js 18+
- Neon Postgres connection string in `DATABASE_URL`
- Clerk app for guardian authentication
- Anthropic API key for Claude, or Ollama running locally for local mode
- Optional PaddleOCR runtime for screenshot OCR

## Environment

Backend `.env` at repo root:

```dotenv
DATABASE_URL=postgresql://...

CLERK_ISSUER=https://your-clerk-issuer.clerk.accounts.dev
# Optional if CLERK_ISSUER is present:
CLERK_JWKS_URL=https://your-clerk-issuer.clerk.accounts.dev/.well-known/jwks.json

WATCHIT_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
WATCHIT_ANTHROPIC_MODEL=claude-3-5-sonnet-latest

WATCHIT_OLLAMA_MODEL=llama3.1
WATCHIT_OLLAMA_BASE_URL=http://localhost:11434

WATCHIT_PROCESSING_MODE=async
WATCHIT_EMBEDDED_AGENT_WORKER=true
WATCHIT_ENABLE_OCR=true
WATCHIT_PARENT_PIN=123456
```

Dashboard `apps/dashboard/.env.local`:

```dotenv
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_...
NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in
NEXT_PUBLIC_CLERK_SIGN_UP_URL=/sign-up
```

## Run Locally

Install Python dependencies:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel setuptools
pip install -r requirements.txt
```

Install dashboard dependencies:

```bash
npm install
```

Run API with embedded worker:

```bash
make run-api
```

Run worker separately:

```bash
WATCHIT_EMBEDDED_AGENT_WORKER=false make run-api
make run-agent-worker
```

Run dashboard:

```bash
make run-dashboard
```

Load the browser extension from `apps/browser-extension` through `chrome://extensions` in developer mode. Update `const API` in `apps/browser-extension/background.js` if your API is not running at `http://127.0.0.1:4849`.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `DATABASE_URL` | Neon/Postgres connection string | required |
| `CLERK_ISSUER` | Clerk issuer used to verify backend bearer tokens | recommended |
| `CLERK_JWKS_URL` | Explicit Clerk JWKS URL | derived from issuer when possible |
| `WATCHIT_PROCESSING_MODE` | `async` queue mode or `sync` compatibility mode | `async` |
| `WATCHIT_EMBEDDED_AGENT_WORKER` | Run worker inside the API process for local development | `true` |
| `WATCHIT_AGENT_WORKER_POLL_INTERVAL` | Worker queue polling interval in seconds | `0.5` |
| `WATCHIT_URL_DECISION_CACHE_ENABLED` | Reuse recent high-confidence URL decisions | `true` |
| `WATCHIT_URL_DECISION_CACHE_TTL_SECONDS` | URL decision cache TTL | `86400` |
| `WATCHIT_URL_DECISION_CACHE_MIN_CONFIDENCE` | Minimum confidence required before caching pipeline decisions | `0.85` |
| `WATCHIT_LLM_PROVIDER` | `anthropic`, `ollama`, or `openai` | `ollama` |
| `ANTHROPIC_API_KEY` | Claude API key | unset |
| `WATCHIT_ANTHROPIC_MODEL` | Claude model | `claude-3-5-sonnet-latest` |
| `WATCHIT_OLLAMA_MODEL` | Local fallback model | `qwen2.5:7b-instruct-q4_K_M` |
| `WATCHIT_OLLAMA_BASE_URL` | Ollama endpoint | `http://localhost:11434` |
| `WATCHIT_ENABLE_OCR` | Enable screenshot OCR | `true` |
| `WATCHIT_SAVE_SCREENSHOTS` | Persist screenshots to disk | `false` |
| `WATCHIT_PARENT_PIN` | PIN for pause/resume controls | `123456` |
| `WATCHIT_LOG_LEVEL` | Python API/worker structured log level and dashboard server log level | `info` |
| `LOG_LEVEL` | Fallback server log level | `info` |
| `WATCHIT_AGENT_TRACE_FILES` | Write detailed local agent trace files under `logs/sessions` | `false` |

## API Surface

- `POST /v1/event`: enqueue a browser event.
- `POST /v1/event/upgrade`: enqueue screenshot/OCR data for an existing event.
- `GET /v1/stream/decisions`: stream decisions over SSE.
- `GET /v1/events`: fetch recent events.
- `GET /v1/decisions`: fetch recent decisions.
- `GET /v1/children`: list child profiles.
- `POST /v1/children/{child_id}/settings`: update child strictness and age.
- `POST /v1/decisions/{decision_id}/override`: store guardian correction.
- `POST /v1/control/pause`: pause enforcement with parent PIN.
- `POST /v1/control/resume`: resume enforcement with parent PIN.

Guardian/admin endpoints require Clerk bearer tokens. Extension ingest endpoints still need a production device-token model before public deployment.

## Deployment Direction

- Dashboard: Vercel.
- API: Vercel Functions or Render web service, depending on runtime needs.
- Worker host: Render background worker for `services/agent-worker`.
- Database: Neon Postgres with `pgvector`.
- Auth: Clerk.
- LLM: Claude primary with local Ollama/Llama fallback where available.

The worker host is simply the platform that runs background processing outside HTTP requests. Render is the current choice because the agent worker can run as a long-lived process with OCR/model dependencies while sharing the same Neon `DATABASE_URL` as the API.

## Logging

- Dashboard server-side logs use Pino and print structured JSON to stdout.
- Python API and worker logs use `structlog` and print structured JSON to stdout.
- API request logs include request ID, method, path, status, and duration.
- Worker logs include job claim/start/complete/failure, queue wait time, processing duration, event IDs, OCR requests, decisions, and model lifecycle events.
- Pipeline timing logs include database write, pause check, profile load, cache lookup, graph, analysis writes, policy, decision write, cache write, publish, and total duration.
- URL decision cache logs emit cache hit/miss/store/update events with normalized URL and cache key.
- Dashboard client-side logs print through `console.log`/`console.warn`/`console.error`.
- Client logs are also persisted in browser `localStorage` as recent JSON entries and can be downloaded from the dashboard with **Export logs**.
- Detailed agent trace files are opt-in with `WATCHIT_AGENT_TRACE_FILES=true`.
- A log drain is intentionally not configured yet.

## Development Notes

- `make run` aliases `make run-api`.
- `make run-agent-worker` starts the standalone background processor.
- `make run-dashboard` starts the Next.js dashboard.
- `packages/core/src/watchit_core/db.py` initializes the current Postgres schema and enables `pgvector`.
- Before production, replace schema auto-creation with migrations and add a dedicated extension device credential.
