from __future__ import annotations
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel
from typing import Literal, Optional
from app.api_models import EventInput
from core.db import db
from core.config import settings
from runtime.bootstrap import process_event, bus, publish_decision_row
from runtime.guardian_learning import GuardianLearningLoop
from core import pg

import logging
from core.activity_logger import log_service_event, log_service_shutdown

logger = logging.getLogger("watchit.api")

app = FastAPI(title="WatchIt Local API", version="0.2.0", description="Local-only parental monitoring with PaddleOCR and predictive blocking")
_learning_loop: GuardianLearningLoop | None = None
_learning_task: asyncio.Task | None = None

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:4848","http://localhost:4848"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup():
    log_service_event("api_startup")
    global _learning_loop, _learning_task
    if _learning_task is None:
        _learning_loop = GuardianLearningLoop()
        _learning_task = asyncio.create_task(_learning_loop.run_forever())


@app.on_event("shutdown")
async def _shutdown():
    log_service_shutdown({"service": "api"})
    global _learning_task
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

async def sync_pg_on_demand():
    if not settings.pg_dsn:
        return
    from runtime.pg_replicator import sync_once_on_demand
    sync_once_on_demand()

@app.post("/v1/event")
async def post_event(evt: EventInput):
    try:
        return await process_event(evt.model_dump(), upgrade=False)
    except Exception:
        logger.exception("Error in /v1/event")
        raise HTTPException(500, "internal error")

@app.post("/v1/event/upgrade")
async def post_event_upgrade(evt: UpgradeInput):
    try:
        return await process_event(evt.model_dump(), upgrade=True)
    except Exception:
        logger.exception("Error in /v1/event/upgrade")
        raise HTTPException(500, "internal error")

@app.get("/v1/events")
async def get_events(child_id: str | None = None, limit: int = 50):
    events = None
    if settings.pg_dsn:
        await sync_pg_on_demand()
        try:
            events = pg.fetch_recent_events(child_id, limit)
        except Exception as e:
            logger.warning("Falling back to SQLite for events: %s", e)
    if events is None:
        events = db.get_recent_events(child_id, limit)
    return {"events": events}

@app.get("/v1/decisions")
async def get_decisions(child_id: str | None = None, limit: int = 50):
    decisions = None
    if settings.pg_dsn:
        await sync_pg_on_demand()
        try:
            decisions = pg.fetch_recent_decisions(child_id, limit)
        except Exception as e:
            logger.warning("Falling back to SQLite for decisions: %s", e)
    if decisions is None:
        decisions = db.get_recent_decisions(child_id, limit)
    return {"decisions": decisions}

@app.get("/v1/stream/decisions")
async def stream_decisions():
    from app.sse import sse_generator
    q = bus.subscribe()
    return StreamingResponse(
        sse_generator(q),
        media_type="text/event-stream",
        background=BackgroundTask(bus.unsubscribe, q),
    )

@app.post("/v1/control/pause")
async def control_pause(body: PausePayload):
    if body.pin != settings.parent_pin:
        raise HTTPException(403, "Invalid PIN")
    import time
    # If minutes not provided or <=0, treat as an indefinite pause (10-year horizon).
    minutes = body.minutes if body.minutes is not None else 0
    horizon_minutes = minutes if minutes > 0 else 10 * 365 * 24 * 60
    until_ms = int(time.time()*1000 + horizon_minutes*60*1000)
    cur = db.conn.cursor()
    cur.execute("INSERT INTO settings(key,value) VALUES('paused_until', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(until_ms),))
    db.conn.commit()
    log_service_event(
        "monitor_paused",
        {"minutes_requested": minutes, "effective_minutes": horizon_minutes, "paused_until_ms": until_ms},
    )
    return {"ok": True, "paused_until": until_ms}

@app.post("/v1/control/resume")
async def control_resume(body: ResumePayload):
    cur = db.conn.cursor()
    cur.execute("DELETE FROM settings WHERE key='paused_until'")
    db.conn.commit()
    log_service_event("monitor_resumed")
    return {"ok": True}

@app.get("/v1/children")
async def list_children():
    await sync_pg_on_demand()
    try:
        children = pg.fetch_children()
    except Exception as e:
        raise HTTPException(503, f"Postgres unavailable: {e}")
    return {"children": children, "active_child_id": db.get_active_child_id()}

@app.post("/v1/children/{child_id}/settings")
async def update_child(child_id: str, payload: ChildSettingsPayload):
    if payload.age is not None and (payload.age < 3 or payload.age > 18):
        raise HTTPException(400, "age must be between 3 and 18")
    if payload.strictness is None and payload.age is None:
        raise HTTPException(400, "provide strictness and/or age")
    db.add_child_profile(child_id)
    db.update_child_profile(child_id, strictness=payload.strictness, age=payload.age)
    profile = db.get_child_profile(child_id) or {}
    db.set_active_child_id(child_id)
    if settings.pg_dsn:
        try:
            pg.upsert_child(child_id, strictness=payload.strictness, age=payload.age)
        except Exception as e:
            raise HTTPException(500, f"Failed to sync to Postgres: {e}")
    return {"child": profile}

@app.post("/v1/decisions/{decision_id}/override")
async def override_decision(decision_id: str, payload: DecisionOverridePayload):
    record = db.override_decision(decision_id, payload.action)
    if not record:
        raise HTTPException(404, "decision not found")
    await publish_decision_row(record)
    # Refresh guardian feedback immediately so overrides influence subsequent LLM calls.
    global _learning_loop
    if _learning_loop:
        try:
            await _learning_loop.process_once()
        except Exception:
            logger.exception("Failed to refresh guardian feedback after override")
    return {"decision": record}
