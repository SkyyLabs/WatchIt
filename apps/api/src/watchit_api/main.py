from __future__ import annotations
import asyncio
import time
import uuid
from fastapi import Depends, FastAPI, HTTPException
from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.responses import Response
from pydantic import BaseModel
from typing import Literal, Optional
from watchit_api.schemas import EventInput
from watchit_core.db import db
from watchit_core.config import settings
from watchit_agents.runtime import process_event, bus, publish_decision_row
from watchit_agents.worker import AgentWorker
from watchit_learning.guardian_learning import GuardianLearningLoop
from watchit_api.auth import require_guardian, require_guardian_stream
from watchit_core.logging import bind_log_context, clear_log_context, configure_logging, get_logger
from watchit_core.url_cache import url_cache_key

from watchit_core.activity_logger import log_service_event, log_service_shutdown

configure_logging("api")
logger = get_logger("watchit.api")

app = FastAPI(title="WatchIt Local API", version="0.2.0", description="Local-only parental monitoring with PaddleOCR and predictive blocking")
_learning_loop: GuardianLearningLoop | None = None
_learning_task: asyncio.Task | None = None
_agent_worker: AgentWorker | None = None
_agent_worker_task: asyncio.Task | None = None

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:4848","http://localhost:4848"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex}"
    start = time.perf_counter()
    clear_log_context()
    bind_log_context(
        service="api",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )
    logger.info("request_started")
    status_code = 500
    try:
        response: Response = await call_next(request)
        status_code = response.status_code
        response.headers["x-request-id"] = request_id
        return response
    except Exception:
        logger.exception("request_failed")
        raise
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.info("request_finished", status_code=status_code, duration_ms=duration_ms)
        clear_log_context()


@app.on_event("startup")
async def _startup():
    log_service_event("api_startup")
    logger.info(
        "api_startup",
        processing_mode=settings.processing_mode,
        embedded_agent_worker=settings.embedded_agent_worker,
    )
    db.connect()
    global _learning_loop, _learning_task, _agent_worker, _agent_worker_task
    if _learning_task is None:
        _learning_loop = GuardianLearningLoop()
        _learning_task = asyncio.create_task(_learning_loop.run_forever())
    if settings.processing_mode == "async" and settings.embedded_agent_worker and _agent_worker_task is None:
        _agent_worker = AgentWorker()
        _agent_worker_task = asyncio.create_task(_agent_worker.run_forever())
        logger.info("embedded_agent_worker_started")


@app.on_event("shutdown")
async def _shutdown():
    log_service_shutdown({"service": "api"})
    logger.info("api_shutdown")
    global _learning_task, _agent_worker_task
    if _agent_worker_task:
        _agent_worker_task.cancel()
        try:
            await _agent_worker_task
        except asyncio.CancelledError:
            pass
        _agent_worker_task = None
    if _learning_task:
        _learning_task.cancel()
        try:
            await _learning_task
        except asyncio.CancelledError:
            pass
        _learning_task = None

class PinPayload(BaseModel):
    pin: str

class PausePayload(BaseModel):
    pin: str
    minutes: Optional[int] = None

class ResumePayload(BaseModel):
    pin: Optional[str] = None

class UpgradeInput(EventInput):
    id: str  # existing event_id

class ChildSettingsPayload(BaseModel):
    strictness: Optional[Literal["lenient","standard","strict"]] = None
    age: Optional[int] = None

class DecisionOverridePayload(BaseModel):
    action: Literal["allow","warn","blur","block","notify"]

@app.post("/v1/event")
async def post_event(evt: EventInput):
    try:
        event = evt.model_dump()
        if settings.processing_mode == "sync":
            logger.info("event_processing_sync", child_id=event.get("child_id"), tab_id=event.get("tab_id"), url=event.get("url"))
            return await process_event(event, upgrade=False)
        job_id, event_id = db.enqueue_event_job(event, upgrade=False)
        logger.info("event_queued", job_id=job_id, event_id=event_id, child_id=event.get("child_id"), upgrade=False)
        return {"status": "queued", "job_id": job_id, "event_id": event_id, "action": "pending", "needs_ocr": False}
    except Exception:
        logger.exception("event_enqueue_failed")
        raise HTTPException(500, "internal error")

@app.post("/v1/event/upgrade")
async def post_event_upgrade(evt: UpgradeInput):
    try:
        event = evt.model_dump()
        if settings.processing_mode == "sync":
            logger.info("event_upgrade_processing_sync", event_id=event.get("id"), child_id=event.get("child_id"))
            return await process_event(event, upgrade=True)
        job_id, event_id = db.enqueue_event_job(event, upgrade=True)
        logger.info("event_queued", job_id=job_id, event_id=event_id, child_id=event.get("child_id"), upgrade=True)
        return {"status": "queued", "job_id": job_id, "event_id": event_id, "action": "pending", "needs_ocr": False}
    except Exception:
        logger.exception("event_upgrade_enqueue_failed")
        raise HTTPException(500, "internal error")

@app.get("/v1/events")
async def get_events(child_id: str | None = None, limit: int = 50, _guardian=Depends(require_guardian)):
    logger.info("events_requested", child_id=child_id, limit=limit)
    return {"events": db.get_recent_events(child_id, limit)}

@app.get("/v1/decisions")
async def get_decisions(child_id: str | None = None, limit: int = 50, _guardian=Depends(require_guardian)):
    logger.info("decisions_requested", child_id=child_id, limit=limit)
    return {"decisions": db.get_recent_decisions(child_id, limit)}

@app.get("/v1/stream/decisions")
async def stream_decisions(_guardian=Depends(require_guardian_stream)):
    from watchit_api.sse import sse_generator
    q = bus.subscribe()
    logger.info("decision_stream_subscribed")
    return StreamingResponse(
        sse_generator(q),
        media_type="text/event-stream",
        background=BackgroundTask(bus.unsubscribe, q),
    )

@app.post("/v1/control/pause")
async def control_pause(body: PausePayload, _guardian=Depends(require_guardian)):
    if body.pin != settings.parent_pin:
        raise HTTPException(403, "Invalid PIN")
    import time
    # If minutes not provided or <=0, treat as an indefinite pause (10-year horizon).
    minutes = body.minutes if body.minutes is not None else 0
    horizon_minutes = minutes if minutes > 0 else 10 * 365 * 24 * 60
    until_ms = int(time.time()*1000 + horizon_minutes*60*1000)
    db.set_setting("paused_until", str(until_ms))
    logger.info(
        "monitor_paused",
        minutes_requested=minutes,
        effective_minutes=horizon_minutes,
        paused_until_ms=until_ms,
    )
    log_service_event(
        "monitor_paused",
        {"minutes_requested": minutes, "effective_minutes": horizon_minutes, "paused_until_ms": until_ms},
    )
    return {"ok": True, "paused_until": until_ms}

@app.post("/v1/control/resume")
async def control_resume(body: ResumePayload, _guardian=Depends(require_guardian)):
    db.delete_setting("paused_until")
    log_service_event("monitor_resumed")
    logger.info("monitor_resumed")
    return {"ok": True}

@app.get("/v1/children")
async def list_children(_guardian=Depends(require_guardian)):
    children = db.fetch_children()
    logger.info("children_requested", count=len(children))
    return {"children": children, "active_child_id": db.get_active_child_id()}

@app.post("/v1/children/{child_id}/settings")
async def update_child(child_id: str, payload: ChildSettingsPayload, _guardian=Depends(require_guardian)):
    if payload.age is not None and (payload.age < 3 or payload.age > 18):
        raise HTTPException(400, "age must be between 3 and 18")
    if payload.strictness is None and payload.age is None:
        raise HTTPException(400, "provide strictness and/or age")
    db.add_child_profile(child_id)
    db.update_child_profile(child_id, strictness=payload.strictness, age=payload.age)
    profile = db.get_child_profile(child_id) or {}
    db.set_active_child_id(child_id)
    logger.info("child_settings_updated", child_id=child_id, strictness=payload.strictness, age=payload.age)
    return {"child": profile}

@app.post("/v1/decisions/{decision_id}/override")
async def override_decision(decision_id: str, payload: DecisionOverridePayload, _guardian=Depends(require_guardian)):
    record = db.override_decision(decision_id, payload.action)
    if not record:
        logger.warning("decision_override_missing", decision_id=decision_id, action=payload.action)
        raise HTTPException(404, "decision not found")
    await publish_decision_row(record)
    logger.info("decision_override_saved", decision_id=decision_id, action=payload.action)
    if settings.url_decision_cache_enabled and record.get("url"):
        profile = db.get_child_profile(record.get("child_id")) if record.get("child_id") else None
        normalized_url, cache_key = url_cache_key(
            url=record.get("url"),
            child_id=record.get("child_id"),
            strictness=(profile or {}).get("strictness"),
            age=(profile or {}).get("age"),
            policy_version=settings.policy_version,
        )
        details = dict(record.get("details_json") or {})
        details.update(
            {
                "confidence": 1.0,
                "manual_override": True,
                "original_action": record.get("original_action"),
            }
        )
        db.upsert_url_decision_cache(
            cache_key=cache_key,
            normalized_url=normalized_url,
            child_id=record.get("child_id"),
            strictness=(profile or {}).get("strictness"),
            age=(profile or {}).get("age"),
            policy_version=settings.policy_version,
            action=payload.action,
            reason="manual_override",
            details=details,
            source_decision_id=decision_id,
            source="manual_override",
        )
        logger.info(
            "url_decision_cache_updated_from_override",
            decision_id=decision_id,
            cache_key=cache_key,
            normalized_url=normalized_url,
            action=payload.action,
        )
    # Refresh guardian feedback immediately so overrides influence subsequent LLM calls.
    global _learning_loop
    if _learning_loop:
        try:
            await _learning_loop.process_once()
        except Exception:
            logger.exception("guardian_feedback_refresh_failed", decision_id=decision_id)
    return {"decision": record}
