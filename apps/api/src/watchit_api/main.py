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
from watchit_api.auth import require_device, require_device_stream, require_guardian, require_guardian_stream
from watchit_core.logging import bind_log_context, clear_log_context, configure_logging, get_logger
from watchit_core.url_cache import url_cache_key

from watchit_core.activity_logger import log_service_event, log_service_shutdown

configure_logging("api")
logger = get_logger("watchit.api")

app = FastAPI(title="WatchIt Local API", version="0.2.0", description="Local-only parental monitoring with Docling OCR and predictive blocking")
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

@app.post("/v1/event")
async def post_event(evt: EventInput, device_ctx=Depends(require_device)):
    try:
        device = device_ctx["device"]
        event = evt.model_dump()
        event["household_id"] = device["household_id"]
        event["child_id"] = device["child_id"]
        event["device_id"] = device["id"]
        session = db.get_active_monitoring_session(device["household_id"], device["child_id"], device["id"])
        if not session:
            logger.info("event_ignored_no_active_session", child_id=device["child_id"], device_id=device["id"])
            raise HTTPException(409, "monitoring is not active for this device")
        event["session_id"] = session["id"]
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
async def post_event_upgrade(evt: UpgradeInput, device_ctx=Depends(require_device)):
    try:
        device = device_ctx["device"]
        event = evt.model_dump()
        event["household_id"] = device["household_id"]
        event["child_id"] = device["child_id"]
        event["device_id"] = device["id"]
        session = db.get_active_monitoring_session(device["household_id"], device["child_id"], device["id"])
        if not session:
            logger.info("event_upgrade_ignored_no_active_session", child_id=device["child_id"], device_id=device["id"])
            raise HTTPException(409, "monitoring is not active for this device")
        event["session_id"] = session["id"]
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
async def get_events(child_id: str | None = None, limit: int = 50, guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    logger.info("events_requested", child_id=child_id, limit=limit)
    return {"events": db.get_recent_events(child_id, limit, household_id)}

@app.get("/v1/decisions")
async def get_decisions(child_id: str | None = None, limit: int = 50, guardian_ctx=Depends(require_guardian)):
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
    household_id = device_ctx["device"]["household_id"]
    q = bus.subscribe()
    logger.info("device_decision_stream_subscribed")
    return StreamingResponse(
        sse_generator(q, household_id=household_id),
        media_type="text/event-stream",
        background=BackgroundTask(bus.unsubscribe, q),
    )

@app.get("/v1/settings/security")
async def get_security_settings(guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    return {"parent_pin_set": db.is_parent_pin_set(household_id), "pin_policy": PIN_POLICY}

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

@app.get("/v1/children")
async def list_children(guardian_ctx=Depends(require_guardian)):
    household_id = guardian_ctx["household"]["id"]
    children = db.fetch_children(household_id)
    logger.info("children_requested", count=len(children))
    return {"children": children, "active_child_id": db.get_active_child_id(household_id)}

@app.get("/v1/children/{child_id}/devices")
async def list_child_devices(child_id: str, guardian_ctx=Depends(require_guardian)):
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

@app.post("/v1/device/redeem")
async def redeem_pairing_code(payload: PairingRedeemPayload):
    result = db.redeem_pairing_code(
        payload.code,
        install_id=payload.install_id,
        device_name=payload.device_name or "",
        browser_name=payload.browser_name or "",
        browser_version=payload.browser_version or "",
        extension_version=payload.extension_version or "",
    )
    if not result:
        raise HTTPException(400, "invalid or expired pairing code")
    device = result["device"]
    db.log_audit(device["household_id"], "device_paired", device_id=device["id"], entity_type="device", entity_id=device["id"])
    return {"device": device, "device_token": result["device_token"]}

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
