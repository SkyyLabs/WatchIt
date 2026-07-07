# API rules

Applies when touching `apps/api/src/watchit_api/` (`main.py`, `auth.py`, `schemas.py`, `sse.py`).

## Two auth models, never mixed

Both live in `auth.py`.

- **Guardian dashboard/admin endpoints** depend on `require_guardian`: Clerk bearer token → resolves `guardians` / `households` / `household_members`. The first authenticated guardian may claim the legacy household if it has no members.
- **Extension ingest endpoints** depend on `require_device`: paired device token → derives `household_id` / `child_id` / `device_id`.

Pick the right dependency for every new route. Never authenticate an extension path with a guardian token or vice versa.

## Scope everything by authenticated context

Never trust client-submitted `child_id` / `household_id`. Extension events scope from the device token; guardian queries scope from the resolved household. This includes SSE — `sse.py` must preserve household filtering on the decision stream.

The SSE decision bus is **in-memory by default** (`WATCHIT_SSE_BUS=memory`) — single API instance, embedded worker only. `WATCHIT_SSE_BUS=postgres` switches to pg_notify fan-out: publish via `db.notify_decision`, each API instance runs a LISTEN task bridging into its local bus. Required for multiple API replicas or a standalone worker whose decisions must reach SSE subscribers.

## Route design

- Pydantic models (in `schemas.py`) for all request bodies.
- Status codes: invalid/missing auth → 401, forbidden scope → 403, missing resource → 404, inactive monitoring → 409.
- Raise `HTTPException` with the right status; avoid broad `except`.
- Keep ingest fast — queue heavy work (OCR/LLM), don't block the request. Only `WATCHIT_PROCESSING_MODE=sync` runs the pipeline inline.

## Endpoint surface

`POST /v1/event` (enqueue), `POST /v1/event/upgrade` (screenshot/OCR re-enqueue), `GET /v1/stream/decisions` (SSE), `GET /v1/events`, `GET /v1/decisions`, `GET /v1/children`, `POST /v1/children/{id}/settings`, `POST /v1/decisions/{id}/override`, `GET /v1/settings/security`, `POST /v1/settings/parent-pin`, `POST /v1/control/pause`, `POST /v1/control/resume`.

## Security

Never log secrets, bearer tokens, device tokens, pairing codes, or raw screenshots. Store token and PIN hashes only. Pairing codes are short-lived and one-time use.
