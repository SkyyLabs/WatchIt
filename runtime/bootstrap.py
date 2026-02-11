from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Dict, Any, Optional
from core.db import db
from core.config import settings
from core.activity_logger import log_step, log_service_event
from analysis.graph import app_graph, MonitorState
from policy.engine import PolicyEngine, _paused_until
from core.screenshot_store import persist_screenshots_async

class DecisionBus:
    def __init__(self):
        self._subs = set()

    def subscribe(self):
        q = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q):
        self._subs.discard(q)

    async def publish(self, message: Dict[str, Any]):
        for q in list(self._subs):
            await q.put(message)

bus = DecisionBus()
policy = PolicyEngine()
logger = logging.getLogger("watchit.bootstrap")


def _extract_screenshots(event: Dict[str, Any]) -> list[str]:
    payload = event.get("data_json")
    if not payload:
        return []
    try:
        parsed = json.loads(payload) or {}
    except Exception:
        return []
    shots = parsed.get("screenshots_b64")
    if not isinstance(shots, list):
        return []
    return [s for s in shots if isinstance(s, str) and s]


def _schedule_screenshot_save(event_id: str, event: Dict[str, Any]) -> None:
    if not settings.save_screenshots:
        return
    screenshots = _extract_screenshots(event)
    if not screenshots:
        return
    metadata = {
        "event_id": event_id,
        "child_id": event.get("child_id"),
        "ts": event.get("ts"),
        "url": event.get("url"),
        "title": event.get("title"),
        "kind": event.get("kind"),
    }

    async def _runner():
        await persist_screenshots_async(event_id, screenshots, metadata)

    task = asyncio.create_task(_runner())

    def _finished(t: asyncio.Task) -> None:
        try:
            t.result()
        except Exception:
            logger.exception("Screenshot persistence task failed for event %s", event_id)

    task.add_done_callback(_finished)


def _format_decision_message(
    decision_id: str,
    event: Dict[str, Any],
    decision_payload: Dict[str, Any],
    *,
    confidence: float,
    need_screenshot: bool,
    headline_result: Dict[str, Any] | None,
    llm_rationale: str | None,
) -> Dict[str, Any]:
    return {
        "decision_id": decision_id,
        "event_id": event.get("id"),
        **decision_payload,
        "upgrade": False,
        "needs_ocr": need_screenshot,
        "confidence": confidence,
        "tab_id": event.get("tab_id"),
        "url": event.get("url"),
        "title": event.get("title"),
        "headline_agent": headline_result,
        "ts": event.get("ts"),
        "child_id": event.get("child_id"),
        "manual_flagged": False,
        "manual_action": None,
        "original_action": decision_payload.get("action"),
        "llm_rationale": llm_rationale,
    }


def _decision_message_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    details = row.get("details_json") or {}
    return {
        "decision_id": row.get("id"),
        "event_id": row.get("event_id"),
        "tab_id": row.get("tab_id"),
        "action": row.get("action"),
        "reason": row.get("reason"),
        "categories": details.get("categories", []),
        "upgrade": False,
        "needs_ocr": False,
        "confidence": details.get("confidence", 1.0),
        "llm_rationale": details.get("rationale"),
        "url": row.get("url"),
        "title": row.get("title"),
        "headline_agent": None,
        "ts": row.get("ts"),
        "child_id": row.get("child_id"),
        "manual_flagged": bool(row.get("manual_flagged")),
        "manual_action": row.get("manual_action"),
        "original_action": row.get("original_action") or row.get("action"),
    }


async def publish_decision_row(row: Dict[str, Any]) -> None:
    message = _decision_message_from_row(row)
    await bus.publish(message)

async def process_event(event: Dict[str, Any], *, upgrade: bool = False) -> Dict[str, Any]:
    active_child = db.get_active_child_id()
    if active_child:
        event["child_id"] = active_child
    event_id = event.get("id")
    if not upgrade or not event_id:
        event_id = db.add_event(event)
        event["id"] = event_id
    else:
        db.update_event_data_json(event_id, event.get("data_json") or "")

    # Global pause gate: short-circuit the pipeline while paused.
    now_ms = int(time.time() * 1000)
    paused_until = _paused_until()
    if paused_until and now_ms < paused_until:
        log_service_event(
            "pipeline_bypassed_paused",
            {"event_id": event_id, "child_id": event.get("child_id"), "paused_until_ms": paused_until},
        )
        log_step("event_received", event, {"upgrade": upgrade, "paused": True})
        decision = {"action": "allow", "reason": "paused", "categories": []}
        confidence = 1.0
        decision_id = db.add_decision(
            event_id,
            settings.policy_version,
            decision["action"],
            decision["reason"],
            {"categories": decision.get("categories", []), "confidence": confidence},
        )
        message = _format_decision_message(
            decision_id,
            event,
            decision,
            confidence=confidence,
            need_screenshot=False,
            headline_result=None,
            llm_rationale=None,
        )
        message["upgrade"] = bool(upgrade)
        log_step("decision_finalized", event, {"decision": decision, "confidence": confidence, "headline_agent": None})
        await bus.publish(message)
        return message

    child_id = event.get("child_id")
    profile = db.get_child_profile(child_id) if child_id else None
    if not profile and child_id:
        db.add_child_profile(child_id)
        profile = db.get_child_profile(child_id)
    if not profile:
        profile = {"id": child_id or "child_default", "strictness": "standard", "age": 12}

    _schedule_screenshot_save(str(event_id), event)
    log_step("event_received", event, {"upgrade": upgrade})
    state = MonitorState(event=event, child_profile=profile, is_upgrade=upgrade)
    state = MonitorState(**app_graph.invoke(state))

    db.add_analysis(event_id, "fast+ocr", "1.0", state.fast_scores, label="")
    if state.judge_json:
        db.add_analysis(event_id, "llm_judge", "1.0", state.judge_json, label=state.judge_json.get("action",""))
    if state.headline_result:
        db.add_analysis(event_id, "headline_agent", "1.0", state.headline_result, label=state.headline_result.get("risk",""))

    confidence = state.judge_json.get("confidence", 1.0) if state.judge_json else 1.0
    need_screenshot = settings.enable_ocr and not upgrade and state.needs_screenshot

    if state.final_decision:
        decision = state.final_decision
    elif need_screenshot:
        decision = {"action": "warn", "reason": "pending_ocr", "categories": []}
    else:
        decision = policy.decide(event, state.fast_scores, state.judge_json, profile, state.headline_result)

    llm_rationale = decision.get("llm_rationale") or (state.judge_json or {}).get("rationale")
    if llm_rationale:
        decision["llm_rationale"] = llm_rationale

    decision_id = db.add_decision(
        event_id,
        settings.policy_version,
        decision["action"],
        decision["reason"],
        {
            "categories": decision.get("categories", []),
            "confidence": confidence,
            **({"rationale": llm_rationale} if llm_rationale else {}),
        },
    )

    message = _format_decision_message(
        decision_id,
        event,
        decision,
        confidence=confidence,
        need_screenshot=need_screenshot,
        headline_result=state.headline_result,
        llm_rationale=llm_rationale,
    )
    message["upgrade"] = bool(upgrade)
    log_step("decision_finalized", event, {"decision": decision, "confidence": confidence, "headline_agent": state.headline_result})
    await bus.publish(message)
    return message
