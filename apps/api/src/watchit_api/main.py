from __future__ import annotations
import asyncio
import re
import time
import uuid
from fastapi import Depends, FastAPI, HTTPException
from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.responses import Response
from pydantic import BaseModel
from typing import Literal, Optional
import orjson
from watchit_api.schemas import ClientLogBatch, EventInput
from watchit_core.db import DECISION_CHANNEL, db
from watchit_core.config import settings
from watchit_core.logging import bind_log_context, clear_log_context, configure_logging, get_logger, shutdown_logging

# Configure logging before importing the agent modules: graph.py builds the LLM
# judge/agents at module import and logs `llm_provider_selected` — that line must
# render through the JSON handler, not structlog's default console renderer.
configure_logging("api")
logger = get_logger("watchit.api")

from watchit_agents.runtime import process_event, bus, publish_decision_row, _decision_message_from_row
from watchit_agents.worker import AgentWorker
from watchit_learning.guardian_learning import GuardianLearningLoop
from watchit_api.auth import require_device, require_device_stream, require_guardian, require_guardian_stream
from watchit_core.url_cache import url_cache_key
from watchit_core.policy.engine import PolicyEngine, _in_quiet_hours, _schedule_now
from watchit_core.policy.rules import domain_suffix_match, evaluate_rules, host_of

from watchit_core.activity_logger import log_service_event, log_service_shutdown

# Importing watchit_agents.worker above ran its module-level
# configure_logging("agent-worker"), which (logging already configured) only
# rebinds the structlog service context. Restore "api" so API startup/shutdown
# and other non-request records aren't misclassified as the worker in the drain.
bind_log_context(service="api")

app = FastAPI(title="WatchIt Local API", version="0.2.0", description="Local-only parental monitoring with Docling OCR and predictive blocking")
_learning_loop: GuardianLearningLoop | None = None
_learning_task: asyncio.Task | None = None
_agent_worker: AgentWorker | None = None
_agent_worker_task: asyncio.Task | None = None
_decision_listener_task: asyncio.Task | None = None


async def _deliver_notify_payload(payload: str) -> None:
    try:
        message = orjson.loads(payload)
    except orjson.JSONDecodeError:
        logger.warning("decision_listener_bad_payload")
        return
    await bus.deliver_local(message)


async def _decision_listener() -> None:
    # WATCHIT_SSE_BUS=postgres: bridge pg_notify decision messages into this
    # instance's in-memory bus so SSE subscribers hear decisions published by
    # any API instance or a standalone worker. Reconnects forever on failure.
    import psycopg

    dsn = db.dsn or settings.database_url
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
                await conn.execute(f"LISTEN {DECISION_CHANNEL}")
                logger.info("decision_listener_connected", channel=DECISION_CHANNEL)
                async for notification in conn.notifies():
                    await _deliver_notify_payload(notification.payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("decision_listener_disconnected")
            await asyncio.sleep(2)


from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list(),
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
    logger.debug("request_started")
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
        logger.debug("request_finished", status_code=status_code, duration_ms=duration_ms)
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
    global _learning_loop, _learning_task, _agent_worker, _agent_worker_task, _decision_listener_task
    if _learning_task is None:
        _learning_loop = GuardianLearningLoop()
        _learning_task = asyncio.create_task(_learning_loop.run_forever())
    if settings.processing_mode == "async" and settings.embedded_agent_worker and _agent_worker_task is None:
        _agent_worker = AgentWorker()
        _agent_worker_task = asyncio.create_task(_agent_worker.run_forever())
        logger.info("embedded_agent_worker_started")
    if settings.sse_bus == "postgres" and _decision_listener_task is None:
        _decision_listener_task = asyncio.create_task(_decision_listener())
        logger.info("decision_listener_started")


@app.on_event("shutdown")
async def _shutdown():
    log_service_shutdown({"service": "api"})
    logger.info("api_shutdown")
    global _learning_task, _agent_worker_task, _decision_listener_task
    if _decision_listener_task:
        _decision_listener_task.cancel()
        try:
            await _decision_listener_task
        except asyncio.CancelledError:
            pass
        _decision_listener_task = None
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
    db.close()
    # Drain the async log listener last so shutdown records still reach stdout.
    shutdown_logging()

PIN_POLICY = {"min_length": 4, "max_length": 8, "digits_only": True}


def _valid_parent_pin(pin: str | None) -> bool:
    return bool(pin and pin.isdigit() and PIN_POLICY["min_length"] <= len(pin) <= PIN_POLICY["max_length"])


class ParentPinPayload(BaseModel):
    current_pin: Optional[str] = None
    new_pin: str

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
    name: Optional[str] = None

class ChildCreatePayload(BaseModel):
    child_id: str
    name: str
    strictness: Optional[Literal["lenient","standard","strict"]] = None
    age: Optional[int] = None

class DecisionOverridePayload(BaseModel):
    action: Literal["allow","warn","blur","block","notify"]
    reason: Optional[str] = None

class PairingCodePayload(BaseModel):
    child_id: str
    ttl_minutes: int = 15

class PairingRedeemPayload(BaseModel):
    code: str
    install_id: str
    device_name: Optional[str] = None
    browser_name: Optional[str] = None
    browser_version: Optional[str] = None
    extension_version: Optional[str] = None

class MonitoringPayload(BaseModel):
    child_id: Optional[str] = None
    device_id: Optional[str] = None

class DevicePatchPayload(BaseModel):
    paused_until_minutes: int
    pin: Optional[str] = None

class ControlPatchPayload(BaseModel):
    paused_until_minutes: int
    pin: Optional[str] = None

class HouseholdCreatePayload(BaseModel):
    name: str

class ChildMovePayload(BaseModel):
    target_household_id: str

class MonitoringTogglePayload(BaseModel):
    enabled: bool

class ScheduleUpsertPayload(BaseModel):
    schedule_id: Optional[str] = None
    device_id: Optional[str] = None
    name: Optional[str] = None
    days: str
    quiet_start: str
    quiet_end: str
    enabled: bool = True

@app.post("/v1/event")
async def post_event(evt: EventInput, device_ctx=Depends(require_device)):
    try:
        device = device_ctx["device"]
        event = evt.model_dump()
        event["household_id"] = device["household_id"]
        event["child_id"] = device["child_id"]
        event["device_id"] = device["id"]
        # Protection by default: a paired device is monitored unless a guardian
        # explicitly stopped this child. 409 only reflects that explicit stop.
        session = db.ensure_monitoring_session(device["household_id"], device["child_id"], device["id"])
        if not session:
            logger.info("event_ignored_guardian_stopped", child_id=device["child_id"], device_id=device["id"])
            raise HTTPException(409, "monitoring is not active for this device")
        event["session_id"] = session["id"]
        # Snapshot the household monitoring switch at browse time so a toggle after
        # enqueue can't change how this already-observed visit is handled.
        event["monitoring_enabled"] = db.is_household_monitoring_enabled(device["household_id"])
        if settings.processing_mode == "sync":
            logger.info("event_processing_sync", child_id=event.get("child_id"), tab_id=event.get("tab_id"), url=event.get("url"))
            return await process_event(event, upgrade=False)
        job_id, event_id = db.enqueue_event_job(event, upgrade=False)
        logger.info("event_queued", job_id=job_id, event_id=event_id, child_id=event.get("child_id"), upgrade=False)
        return {"status": "queued", "job_id": job_id, "event_id": event_id, "action": "pending", "needs_ocr": False}
    except HTTPException:
        raise  # deliberate statuses (409 guardian-stopped) must not become 500s
    except Exception:
        logger.exception("event_enqueue_failed")
        raise HTTPException(500, "internal error")

@app.post("/v1/event/upgrade")
async def post_event_upgrade(evt: UpgradeInput, device_ctx=Depends(require_device)):
    device = device_ctx["device"]
    # An upgrade names an existing event id; it must belong to this device's
    # household or a hostile device could rewrite another family's events.
    owner_household = db.get_event_household(evt.id)
    if owner_household is None:
        raise HTTPException(404, "event not found")
    if owner_household != device["household_id"]:
        logger.warning("event_upgrade_cross_household_rejected", event_id=evt.id)
        raise HTTPException(403, "event does not belong to this device's household")
    try:
        event = evt.model_dump()
        event["household_id"] = device["household_id"]
        event["child_id"] = device["child_id"]
        event["device_id"] = device["id"]
        session = db.ensure_monitoring_session(device["household_id"], device["child_id"], device["id"])
        if not session:
            logger.info("event_upgrade_ignored_guardian_stopped", child_id=device["child_id"], device_id=device["id"])
            raise HTTPException(409, "monitoring is not active for this device")
        event["session_id"] = session["id"]
        event["monitoring_enabled"] = db.is_household_monitoring_enabled(device["household_id"])
        if settings.processing_mode == "sync":
            logger.info("event_upgrade_processing_sync", event_id=event.get("id"), child_id=event.get("child_id"))
            return await process_event(event, upgrade=True)
        job_id, event_id = db.enqueue_event_job(event, upgrade=True)
        logger.info("event_queued", job_id=job_id, event_id=event_id, child_id=event.get("child_id"), upgrade=True)
        return {"status": "queued", "job_id": job_id, "event_id": event_id, "action": "pending", "needs_ocr": False}
    except HTTPException:
        raise  # deliberate statuses (409 guardian-stopped) must not become 500s
    except Exception:
        logger.exception("event_upgrade_enqueue_failed")
        raise HTTPException(500, "internal error")

@app.get("/v1/event/{event_id}/decision")
def get_event_decision(event_id: str, device_ctx=Depends(require_device)):
    # The extension polls this after posting a visit so enforcement no longer
    # depends on the in-memory SSE push reaching a (possibly evicted) MV3 worker.
    household_id = device_ctx["device"]["household_id"]
    row = db.get_decision_by_event(event_id, household_id)
    if not row:
        return {"status": "pending"}
    return {"status": "decided", "decision": _decision_message_from_row(row)}

@app.get("/v1/events")
def get_events(child_id: str | None = None, limit: int = 50, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    logger.info("events_requested", child_id=child_id, limit=limit)
    return {"events": db.get_recent_events(child_id, limit, household_id)}

@app.get("/v1/decisions")
def get_decisions(child_id: str | None = None, limit: int = 50, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    logger.info("decisions_requested", child_id=child_id, limit=limit)
    return {"decisions": db.get_recent_decisions(child_id, limit, household_id)}

@app.get("/v1/stream/decisions")
async def stream_decisions(guardian_ctx=Depends(require_guardian_stream)):
    from watchit_api.sse import sse_generator
    household_id = guardian_ctx["household"]["id"]
    q = bus.subscribe()
    logger.info("decision_stream_subscribed")
    return StreamingResponse(
        sse_generator(q, household_id=household_id),
        media_type="text/event-stream",
        background=BackgroundTask(bus.unsubscribe, q),
    )

@app.get("/v1/device/stream/decisions")
async def stream_device_decisions(device_ctx=Depends(require_device_stream)):
    from watchit_api.sse import sse_generator
    device = device_ctx["device"]
    q = bus.subscribe()
    logger.info("device_decision_stream_subscribed")
    return StreamingResponse(
        sse_generator(q, household_id=device["household_id"], device_id=device["id"]),
        media_type="text/event-stream",
        background=BackgroundTask(bus.unsubscribe, q),
    )

@app.get("/healthz")
def healthz():
    # Unauthenticated liveness/readiness for deploy probes. Boolean health only —
    # no counts or ids leak to unauthenticated callers.
    try:
        db.ping()
        return {"ok": True}
    except Exception:
        logger.exception("healthz_db_check_failed")
        raise HTTPException(503, "database unavailable")


@app.get("/v1/queue/stats")
def get_queue_stats(guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    return {
        "queue": db.get_queue_stats(household_id),
        "failed_jobs": db.list_failed_event_jobs(household_id),
    }


@app.get("/v1/devices")
def list_household_devices(guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    return {"devices": db.fetch_household_devices(household_id)}


@app.get("/v1/settings/security")
def get_security_settings(guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    return {
        "parent_pin_set": db.is_parent_pin_set(household_id),
        "pin_policy": PIN_POLICY,
        "monitoring_enabled": db.is_household_monitoring_enabled(household_id),
    }

@app.post("/v1/control/monitoring")
async def set_household_monitoring(payload: MonitoringTogglePayload, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    db.set_household_monitoring_enabled(payload.enabled, household_id, guardian_id)
    db.log_audit(
        household_id,
        "household_monitoring_enabled" if payload.enabled else "household_monitoring_disabled",
        guardian_id=guardian_id,
        entity_type="household_setting",
        entity_id="monitoring_enabled",
    )
    logger.info("household_monitoring_toggled", enabled=payload.enabled)
    return {"ok": True, "monitoring_enabled": payload.enabled}

@app.post("/v1/settings/parent-pin")
async def set_parent_pin(payload: ParentPinPayload, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    if not _valid_parent_pin(payload.new_pin):
        raise HTTPException(400, "pin must be 4-8 digits")
    pin_exists = db.is_parent_pin_set(household_id)
    if pin_exists:
        if not payload.current_pin:
            raise HTTPException(400, "current_pin_required")
        if not db.verify_parent_pin(payload.current_pin, household_id):
            raise HTTPException(403, "Invalid PIN")
    db.set_parent_pin(payload.new_pin, household_id, guardian_id)
    db.log_audit(
        household_id,
        "parent_pin_changed" if pin_exists else "parent_pin_set",
        guardian_id=guardian_id,
        entity_type="household_setting",
        entity_id="parent_pin",
    )
    logger.info("parent_pin_updated", pin_previously_set=pin_exists)
    return {"ok": True, "parent_pin_set": True}

@app.patch("/v1/control")
async def patch_control(body: ControlPatchPayload, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    if body.paused_until_minutes == 0:
        db.delete_setting("paused_until", household_id)
        log_service_event("monitor_resumed")
        logger.info("monitor_resumed")
        return {"ok": True, "paused_until": None}
    if not db.is_parent_pin_set(household_id):
        raise HTTPException(409, "parent_pin_required")
    if not db.verify_parent_pin(body.pin or "", household_id):
        raise HTTPException(403, "Invalid PIN")
    minutes = body.paused_until_minutes
    horizon_minutes = minutes if minutes > 0 else 10 * 365 * 24 * 60
    until_ms = int(time.time() * 1000 + horizon_minutes * 60 * 1000)
    db.set_setting("paused_until", str(until_ms), household_id, guardian_id)
    log_service_event("monitor_paused", {"minutes_requested": minutes, "paused_until_ms": until_ms})
    logger.info("monitor_paused", minutes_requested=minutes, paused_until_ms=until_ms)
    return {"ok": True, "paused_until": until_ms}

client_logger = get_logger("watchit.dashboard-client")


@app.post("/v1/client-logs", status_code=202)
async def ingest_client_logs(payload: ClientLogBatch, guardian_ctx=Depends(require_guardian)):
    # Dashboard browser logs, shipped here so the log drain captures them too.
    # Scoped to the authenticated household; no DB write — just re-emit to stdout.
    household_id = guardian_ctx["household"]["id"]
    for entry in payload.entries:
        method = "warning" if entry.level == "warn" else entry.level
        getattr(client_logger, method)(
            entry.message,
            service="dashboard-client",
            household_id=household_id,
            client_ts=entry.ts,
            context=entry.context,
        )
    return {"ok": True, "count": len(payload.entries)}


@app.get("/v1/households")
def list_households(guardian_ctx=Depends(require_guardian)):
    guardian_id = guardian_ctx["guardian"]["id"]
    return {
        "households": db.list_guardian_households(guardian_id),
        "active_household_id": guardian_ctx["household"]["id"],
    }

@app.post("/v1/households")
async def create_household(payload: HouseholdCreatePayload, guardian_ctx=Depends(require_guardian)):
    guardian_id = guardian_ctx["guardian"]["id"]
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    household = db.create_household(guardian_id, name)
    db.log_audit(household["id"], "household_created", guardian_id=guardian_id, entity_type="household", entity_id=household["id"])
    logger.info("household_created", household_id=household["id"])
    return {"household": household}

@app.post("/v1/children/{child_id}/move")
async def move_child(child_id: str, payload: ChildMovePayload, guardian_ctx=Depends(require_guardian)):
    guardian_id = guardian_ctx["guardian"]["id"]
    child = db.get_child_profile(child_id)
    if not child:
        raise HTTPException(404, "child not found")
    source_household_id = child["household_id"]
    # Guardian must belong to both the child's current household and the target.
    if not db.guardian_in_household(guardian_id, source_household_id):
        raise HTTPException(403, "not a member of the child's household")
    if not db.guardian_in_household(guardian_id, payload.target_household_id):
        raise HTTPException(403, "not a member of the target household")
    if source_household_id == payload.target_household_id:
        return {"ok": True}
    if not db.move_child(child_id, source_household_id, payload.target_household_id):
        raise HTTPException(409, "child could not be moved")
    db.log_audit(source_household_id, "child_moved", guardian_id=guardian_id, entity_type="child", entity_id=child_id, metadata={"to_household_id": payload.target_household_id})
    db.log_audit(payload.target_household_id, "child_received", guardian_id=guardian_id, entity_type="child", entity_id=child_id, metadata={"from_household_id": source_household_id})
    logger.info("child_moved", child_id=child_id, to_household_id=payload.target_household_id)
    return {"ok": True}

@app.get("/v1/guardian/children")
def list_guardian_children(guardian_ctx=Depends(require_guardian)):
    # Every child across the guardian's households, each tagged with household_id +
    # name, so the household view can offer add (move-in) / remove (move-out).
    guardian_id = guardian_ctx["guardian"]["id"]
    return {"children": db.fetch_guardian_children(guardian_id)}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_VALID_DAYS = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}

@app.get("/v1/children/{child_id}/schedules")
def list_child_schedules(child_id: str, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    if not db.get_child_profile(child_id, household_id):
        raise HTTPException(404, "child not found")
    return {"schedules": db.list_schedules(household_id, child_id)}

@app.post("/v1/children/{child_id}/schedules")
async def upsert_child_schedule(child_id: str, payload: ScheduleUpsertPayload, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    if not db.get_child_profile(child_id, household_id):
        raise HTTPException(404, "child not found")
    if not _TIME_RE.match(payload.quiet_start) or not _TIME_RE.match(payload.quiet_end):
        raise HTTPException(400, "quiet_start/quiet_end must be HH:MM")
    days = [d.strip() for d in payload.days.split(",") if d.strip()]
    if not days or any(d not in _VALID_DAYS for d in days):
        raise HTTPException(400, "days must be a comma list of Mon..Sun")
    if payload.device_id and not any(d["id"] == payload.device_id for d in db.fetch_devices(household_id, child_id)):
        raise HTTPException(404, "device not found")
    schedule_id = db.upsert_schedule(
        household_id,
        child_id,
        ",".join(days),
        payload.quiet_start,
        payload.quiet_end,
        device_id=payload.device_id,
        name=payload.name or "Quiet hours",
        enabled=payload.enabled,
        schedule_id=payload.schedule_id,
    )
    db.log_audit(household_id, "schedule_saved", guardian_id=guardian_id, entity_type="child_schedule", entity_id=schedule_id)
    return {"schedule_id": schedule_id}

@app.delete("/v1/schedules/{schedule_id}")
async def delete_child_schedule(schedule_id: str, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    if db.delete_schedule(schedule_id, household_id) == 0:
        raise HTTPException(404, "schedule not found")
    db.log_audit(household_id, "schedule_deleted", guardian_id=guardian_id, entity_type="child_schedule", entity_id=schedule_id)
    return {"ok": True}

@app.get("/v1/children")
def list_children(guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    children = db.fetch_children(household_id)
    logger.info("children_requested", count=len(children))
    return {"children": children, "active_child_id": db.get_active_child_id(household_id)}

@app.get("/v1/children/{child_id}/devices")
def list_child_devices(child_id: str, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    if not db.get_child_profile(child_id, household_id):
        raise HTTPException(404, "child not found")
    return {"devices": db.fetch_devices(household_id, child_id)}

@app.post("/v1/children")
async def create_child(payload: ChildCreatePayload, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    if payload.age is not None and (payload.age < 3 or payload.age > 18):
        raise HTTPException(400, "age must be between 3 and 18")
    display_name = payload.name.strip() if payload.name else ""
    if not display_name:
        raise HTTPException(400, "name is required")
    db.add_child_profile(payload.child_id, household_id=household_id, name=display_name)
    db.update_child_profile(payload.child_id, strictness=payload.strictness, age=payload.age, household_id=household_id, name=display_name)
    profile = db.get_child_profile(payload.child_id, household_id) or {}
    db.set_active_child_id(payload.child_id, household_id, guardian_id)
    db.log_audit(household_id, "child_created", guardian_id=guardian_id, entity_type="child", entity_id=payload.child_id, metadata=payload.model_dump())
    logger.info("child_created", child_id=payload.child_id, strictness=payload.strictness, age=payload.age)
    return {"child": profile}

@app.patch("/v1/children/{child_id}")
async def patch_child(child_id: str, body: ChildSettingsPayload, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    if not db.get_child_profile(child_id, household_id):
        raise HTTPException(404, "child not found")
    if body.age is not None and (body.age < 3 or body.age > 18):
        raise HTTPException(400, "age must be between 3 and 18")
    db.update_child_profile(child_id, strictness=body.strictness, age=body.age, household_id=household_id, name=body.name)
    db.log_audit(household_id, "child_settings_updated", guardian_id=guardian_id, entity_type="child", entity_id=child_id)
    return {"ok": True}

@app.post("/v1/decisions/{decision_id}/override")
async def override_decision(decision_id: str, payload: DecisionOverridePayload, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    record = db.override_decision(decision_id, payload.action, household_id, guardian_id, payload.reason)
    if not record:
        logger.warning("decision_override_missing", decision_id=decision_id, action=payload.action)
        raise HTTPException(404, "decision not found")
    await publish_decision_row(record)
    logger.info("decision_override_saved", decision_id=decision_id, action=payload.action)
    if settings.url_decision_cache_enabled and record.get("url"):
        profile = db.get_child_profile(record.get("child_id"), household_id) if record.get("child_id") else None
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
            household_id=household_id,
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

@app.post("/v1/device/pairing-codes")
async def create_pairing_code(payload: PairingCodePayload, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    child = db.get_child_profile(payload.child_id, household_id)
    if not child:
        raise HTTPException(404, "child not found")
    pairing = db.create_pairing_code(household_id, payload.child_id, guardian_id, payload.ttl_minutes)
    db.log_audit(household_id, "device_pairing_code_created", guardian_id=guardian_id, entity_type="child", entity_id=payload.child_id)
    return {"pairing_code": pairing}

# /v1/device/redeem is unauthenticated (the code is the secret) and the code
# space is 10^6, so brute force must be throttled. In-process sliding window —
# adequate for the current single-instance API; move to a shared store before
# scaling out.
from collections import deque

_REDEEM_WINDOW_SECONDS = 60
_REDEEM_MAX_PER_IP = 10
_REDEEM_MAX_GLOBAL = 100
_redeem_by_ip: dict[str, deque] = {}
_redeem_global: deque = deque()


def _redeem_allowed(client_ip: str, now: float | None = None) -> bool:
    now = now if now is not None else time.monotonic()
    cutoff = now - _REDEEM_WINDOW_SECONDS
    per_ip = _redeem_by_ip.setdefault(client_ip, deque())
    for window in (per_ip, _redeem_global):
        while window and window[0] < cutoff:
            window.popleft()
    if len(per_ip) >= _REDEEM_MAX_PER_IP or len(_redeem_global) >= _REDEEM_MAX_GLOBAL:
        return False
    per_ip.append(now)
    _redeem_global.append(now)
    return True


@app.post("/v1/device/redeem")
async def redeem_pairing_code(payload: PairingRedeemPayload, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not _redeem_allowed(client_ip):
        logger.warning("device_redeem_throttled")
        raise HTTPException(429, "too many pairing attempts, try again later")
    result = db.redeem_pairing_code(
        payload.code,
        install_id=payload.install_id,
        device_name=payload.device_name or "",
        browser_name=payload.browser_name or "",
        browser_version=payload.browser_version or "",
        extension_version=payload.extension_version or "",
    )
    if not result:
        logger.warning("device_redeem_failed")
        raise HTTPException(400, "invalid or expired pairing code")
    device = result["device"]
    db.log_audit(device["household_id"], "device_paired", device_id=device["id"], entity_type="device", entity_id=device["id"])
    if result.get("session"):
        db.log_audit(device["household_id"], "monitoring_autostarted", device_id=device["id"], entity_type="monitoring_session", entity_id=result["session"]["id"], metadata={"child_id": device["child_id"], "trigger": "device_paired"})
        logger.info("monitoring_autostarted_on_pair", device_id=device["id"], session_id=result["session"]["id"])
    reassigned = result.get("reassigned")
    if reassigned:
        # Same install_id was stolen from another child: record it on both sides
        # so the previous owner's guardian has a trail of where the device went.
        db.log_audit(reassigned["from_household_id"], "device_reassigned", device_id=device["id"], entity_type="device", entity_id=device["id"], metadata=reassigned)
        if reassigned["to_household_id"] != reassigned["from_household_id"]:
            db.log_audit(reassigned["to_household_id"], "device_reassigned", device_id=device["id"], entity_type="device", entity_id=device["id"], metadata=reassigned)
        logger.info("device_reassigned", device_id=device["id"], from_child_id=reassigned["from_child_id"], to_child_id=reassigned["to_child_id"])
    return {"device": device, "device_token": result["device_token"]}

@app.post("/v1/device/token/rotate")
async def rotate_device_token(device_ctx=Depends(require_device)):
    device = device_ctx["device"]
    rotated = db.rotate_device_token(device["id"])
    if not rotated:
        raise HTTPException(409, "device is not active")
    updated, new_token = rotated
    db.log_audit(device["household_id"], "device_token_rotated", device_id=device["id"], entity_type="device", entity_id=device["id"])
    logger.info("device_token_rotated", device_id=device["id"])
    expires = updated.get("token_expires_at")
    return {
        "device_token": new_token,
        "token_expires_at": int(expires.timestamp() * 1000) if expires else None,
    }


@app.get("/v1/device/policy")
def get_device_policy(device_ctx=Depends(require_device)):
    device = device_ctx["device"]
    snapshot = db.build_policy_snapshot(device)
    db.stamp_policy_fetch(device["id"])
    logger.info("device_policy_snapshot_served", snapshot_version=snapshot["version"])
    return {"snapshot": snapshot}


class RuleCreatePayload(BaseModel):
    action: Literal["allow", "block"]
    rule_type: Literal["domain", "url", "prefix"]
    pattern: str
    child_id: Optional[str] = None
    device_id: Optional[str] = None
    reason: Optional[str] = None
    expires_in_days: Optional[int] = None


@app.get("/v1/rules")
def list_rules(child_id: str | None = None, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    return {"rules": db.list_rules(household_id, child_id)}


@app.post("/v1/rules")
async def create_rule(payload: RuleCreatePayload, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    pattern = payload.pattern.strip()
    if not pattern:
        raise HTTPException(400, "pattern is required")
    if payload.rule_type == "domain":
        # Store bare lowercase hosts; accept pasted URLs for convenience.
        pattern = pattern.lower()
        if "//" in pattern:
            pattern = pattern.split("//", 1)[1]
        pattern = pattern.split("/", 1)[0].split(":")[0]
        if pattern.startswith("www."):
            pattern = pattern[4:]
        if not pattern or "." not in pattern:
            raise HTTPException(400, "pattern must be a domain")
    if payload.child_id and not db.get_child_profile(payload.child_id, household_id):
        raise HTTPException(404, "child not found")
    if payload.device_id:
        if not payload.child_id:
            raise HTTPException(400, "device-scoped rules need child_id")
        if not any(d["id"] == payload.device_id for d in db.fetch_devices(household_id, payload.child_id)):
            raise HTTPException(404, "device not found")
    expires_at = None
    if payload.expires_in_days is not None:
        if payload.expires_in_days <= 0:
            raise HTTPException(400, "expires_in_days must be positive")
        from datetime import datetime, timedelta, timezone as tz
        expires_at = datetime.now(tz.utc) + timedelta(days=payload.expires_in_days)
    rule = db.create_rule(
        household_id,
        action=payload.action,
        rule_type=payload.rule_type,
        pattern=pattern,
        child_id=payload.child_id,
        device_id=payload.device_id,
        reason=payload.reason,
        expires_at=expires_at,
        created_by_guardian_id=guardian_id,
    )
    db.log_audit(household_id, "rule_created", guardian_id=guardian_id, entity_type="policy_rule", entity_id=rule["id"], metadata={"action": payload.action, "rule_type": payload.rule_type, "pattern": pattern, "child_id": payload.child_id, "device_id": payload.device_id})
    logger.info("rule_created", rule_id=rule["id"], action=payload.action, rule_type=payload.rule_type)
    return {"rule": rule}


@app.delete("/v1/rules/{rule_id}")
async def delete_rule(rule_id: str, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    if db.delete_rule(rule_id, household_id) == 0:
        raise HTTPException(404, "rule not found")
    db.log_audit(household_id, "rule_deleted", guardian_id=guardian_id, entity_type="policy_rule", entity_id=rule_id)
    logger.info("rule_deleted", rule_id=rule_id)
    return {"ok": True}


class RuleTestPayload(BaseModel):
    url: str
    child_id: str
    device_id: Optional[str] = None


_policy_engine = PolicyEngine()


@app.post("/v1/rules/test")
def test_rules(payload: RuleTestPayload, guardian_ctx=Depends(require_guardian)):
    """Dry-run of the deterministic layers for one URL, right now: manual rules
    → quiet hours → cached decisions → static policy lists → 'AI would judge'.
    Read-only — never mutates cache counters or writes decisions."""
    household_id = guardian_ctx["household"]["id"]
    profile = db.get_child_profile(payload.child_id, household_id)
    if not profile:
        raise HTTPException(404, "child not found")
    url = payload.url.strip()
    if not url:
        raise HTTPException(400, "url is required")
    if "://" not in url:
        url = "https://" + url

    rules = db.list_active_rules(household_id, payload.child_id, payload.device_id)
    winning = evaluate_rules(rules, url)
    if winning:
        return {
            "action": winning["action"],
            "layer": "rule",
            "detail": f"{winning['rule_type']} rule for {winning['pattern']}",
            "rule_id": winning["id"],
        }

    for schedule in db.get_effective_quiet_schedules(household_id, payload.child_id, payload.device_id):
        sched_now = _schedule_now({"timezone": schedule.get("timezone")})
        if _in_quiet_hours(sched_now, schedule.get("days") or "", f"{schedule['quiet_start']}-{schedule['quiet_end']}"):
            return {
                "action": "block",
                "layer": "schedule",
                "detail": f"quiet hours {schedule['quiet_start']}–{schedule['quiet_end']} are active now",
            }

    if settings.url_decision_cache_enabled:
        _normalized, cache_key = url_cache_key(
            url=url,
            child_id=payload.child_id,
            strictness=profile.get("strictness"),
            age=profile.get("age"),
            policy_version=settings.policy_version,
        )
        cached = db.peek_url_decision(cache_key, settings.url_decision_cache_ttl_seconds, household_id)
        if cached:
            return {
                "action": cached["action"],
                "layer": "cache",
                "detail": f"recent decision ({cached.get('reason') or 'cached'})",
            }

    host = host_of(url)
    for allowed in _policy_engine.allow_domains:
        if domain_suffix_match(host, allowed):
            return {"action": "allow", "layer": "policy", "detail": f"built-in allowlist ({allowed})"}
    for blocked in _policy_engine.block_domains:
        if domain_suffix_match(host, blocked):
            return {"action": "block", "layer": "policy", "detail": f"built-in blocklist ({blocked})"}

    return {
        "action": "unknown",
        "layer": "ai",
        "detail": "no deterministic rule applies — the AI pipeline would judge this page on visit",
    }


@app.get("/v1/review-queue")
def get_review_queue(child_id: str | None = None, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    return {"items": db.list_review_decisions(household_id, child_id)}


@app.patch("/v1/devices/{device_id}")
async def patch_device(device_id: str, body: DevicePatchPayload, guardian_ctx=Depends(require_guardian)):
    import time
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    if body.paused_until_minutes <= 0:
        db.clear_device_pause(household_id, device_id)
        db.log_audit(household_id, "device_resumed", guardian_id=guardian_id, device_id=device_id, entity_type="device", entity_id=device_id)
        logger.info("device_resumed", device_id=device_id)
        return {"ok": True, "paused_until": None}
    if not db.is_parent_pin_set(household_id):
        raise HTTPException(409, "parent_pin_required")
    if not db.verify_parent_pin(body.pin or "", household_id):
        raise HTTPException(403, "Invalid PIN")
    until_ms = int(time.time() * 1000 + body.paused_until_minutes * 60 * 1000)
    if db.set_device_pause(household_id, device_id, until_ms) == 0:
        raise HTTPException(404, "device not found")
    db.log_audit(household_id, "device_paused", guardian_id=guardian_id, device_id=device_id, entity_type="device", entity_id=device_id, metadata={"minutes": body.paused_until_minutes})
    logger.info("device_paused", device_id=device_id, paused_until_ms=until_ms)
    return {"ok": True, "paused_until": until_ms}

@app.delete("/v1/devices/{device_id}")
async def revoke_device(device_id: str, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    if db.revoke_device(household_id, device_id) == 0:
        raise HTTPException(404, "device not found")
    db.log_audit(household_id, "device_revoked", guardian_id=guardian_id, device_id=device_id, entity_type="device", entity_id=device_id)
    logger.info("device_revoked", device_id=device_id)
    return {"ok": True}

@app.post("/v1/monitoring/start")
async def start_monitoring(payload: MonitoringPayload, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    child_id = payload.child_id or db.get_active_child_id(household_id)
    if not child_id:
        raise HTTPException(400, "child_id is required")
    if not db.get_child_profile(child_id, household_id):
        raise HTTPException(404, "child not found")
    session = db.start_monitoring_session(household_id, child_id, guardian_id, payload.device_id)
    db.log_audit(household_id, "monitoring_started", guardian_id=guardian_id, entity_type="monitoring_session", entity_id=session["id"], metadata={"child_id": child_id, "device_id": payload.device_id})
    return {"session": session}

@app.post("/v1/monitoring/stop")
async def stop_monitoring(payload: MonitoringPayload, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    guardian_id = guardian_ctx["guardian"]["id"]
    stopped = db.stop_monitoring_session(household_id, payload.child_id, guardian_id)
    db.log_audit(household_id, "monitoring_stopped", guardian_id=guardian_id, metadata={"child_id": payload.child_id, "stopped": stopped})
    return {"stopped": stopped}
