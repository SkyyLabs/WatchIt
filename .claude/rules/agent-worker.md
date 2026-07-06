# Agent worker rules

Applies when touching `services/agent-worker/src/watchit_agents/` or `services/learning-worker/`.

## Pipeline

`runtime.py` orchestrates event processing:
pause check → profile load → URL decision-cache lookup → LangGraph (`graph.py`) → analysis + decision writes → SSE publish. Emits per-step timing logs (queue wait, each stage, total).

`graph.py` wires the LangGraph steps in `agents/`: `headlines_agent`, `url_agent`, `ocr_agent`, `policy_agent`. `llm_judge.py` and `safety.py` back the classification/judging steps.

**Routing is deterministic conditional edges — no LLM planner.** Flow: `headline` (cheap: tokens/domain/keyword scores) → confident allow (allowlist) or block short-circuits to `policy`; otherwise → `url_llm` (LLM judge). From `url_llm`: a clearly-harmful judgment (block/high severity) goes straight to `policy`; an ambiguous one escalates to `ocr` only when `WATCHIT_ENABLE_OCR` is set. `ocr` with no screenshots requests an upgrade (runtime emits interim `pending_ocr` = warn, never allow) and re-runs via `POST /v1/event/upgrade`. **False-negative invariants:** never allow on an uncertain cheap signal; `judge_json` is always populated before `policy` (so its default-allow branch is unreachable for real pages); the OCR-pending interim is `warn`, not `allow`.

## Queue

Postgres-backed (`packages/core/src/watchit_core/services/queue.py`) — **not** Redis/RabbitMQ. `worker.py` claims queued jobs. The worker runs embedded in the API for local dev (`WATCHIT_EMBEDDED_AGENT_WORKER=true`) or standalone (`python -m watchit_agents.worker`).

## LLM provider

`llm_provider.py` selects Anthropic / OpenAI-compatible / Ollama from `WATCHIT_LLM_PROVIDER`. Claude is production primary; Ollama is the local fallback. Don't hardcode a provider.

## OCR

Docling only (`ocr_asr.py`, `agents/ocr_agent.py`) — not paddleocr. Triggered by the `POST /v1/event/upgrade` screenshot path.

## Rules

- Preserve URL decision-cache behavior — it avoids re-judging recently-decided URLs above a confidence threshold (`WATCHIT_URL_DECISION_CACHE_*`).
- Import shared DB/config/logging from `watchit_core`; keep graph logic in `watchit_agents`.
- No RAG/vector work unless explicitly requested.
- `services/learning-worker` (`guardian_learning.py`) consumes guardian overrides into learned feedback — keep it separate from the main event pipeline.
