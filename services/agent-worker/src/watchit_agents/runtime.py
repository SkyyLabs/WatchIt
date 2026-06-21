from __future__ import annotations
import asyncio
import json
import time
from typing import Dict, Any, Optional
from watchit_core.db import db
from watchit_core.config import settings
from watchit_core.activity_logger import log_step, log_service_event
from watchit_core.logging import bind_log_context, get_logger
from watchit_core.url_cache import url_cache_key
from watchit_agents.graph import app_graph, MonitorState
from watchit_core.policy.engine import PolicyEngine, _paused_until
from watchit_core.screenshot_store import persist_screenshots_async

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
logger = get_logger("watchit.runtime")


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


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
            logger.info("screenshots_persisted", event_id=event_id, count=len(screenshots))
        except Exception:
            logger.exception("screenshots_persist_failed", event_id=event_id)

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
    logger.info("decision_published", decision_id=message.get("decision_id"), event_id=message.get("event_id"), action=message.get("action"))

async def process_event(event: Dict[str, Any], *, upgrade: bool = False) -> Dict[str, Any]:
    pipeline_started = time.perf_counter()
    db_started = time.perf_counter()
    active_child = db.get_active_child_id()
    if active_child:
        event["child_id"] = active_child
    event_id = event.get("id")
    if not event_id:
        event_id = db.add_event(event)
        event["id"] = event_id
    elif upgrade:
        db.update_event_data_json(event_id, event.get("data_json") or "")
    else:
        db.add_event(event)
    db_event_write_ms = _elapsed_ms(db_started)
    bind_log_context(event_id=event_id, child_id=event.get("child_id"), tab_id=event.get("tab_id"), upgrade=upgrade)
    logger.info("event_processing_started", url=event.get("url"), db_event_write_ms=db_event_write_ms)

    # Global pause gate: short-circuit the pipeline while paused.
    pause_started = time.perf_counter()
    now_ms = int(time.time() * 1000)
    paused_until = _paused_until()
    pause_check_ms = _elapsed_ms(pause_started)
    if paused_until and now_ms < paused_until:
        log_service_event(
            "pipeline_bypassed_paused",
            {"event_id": event_id, "child_id": event.get("child_id"), "paused_until_ms": paused_until},
        )
        logger.info("pipeline_bypassed_paused", paused_until_ms=paused_until, pause_check_ms=pause_check_ms)
        log_step("event_received", event, {"upgrade": upgrade, "paused": True})
        decision = {"action": "allow", "reason": "paused", "categories": []}
        confidence = 1.0
        decision_write_started = time.perf_counter()
        decision_id = db.add_decision(
            event_id,
            settings.policy_version,
            decision["action"],
            decision["reason"],
            {"categories": decision.get("categories", []), "confidence": confidence},
        )
        decision_write_ms = _elapsed_ms(decision_write_started)
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
        publish_started = time.perf_counter()
        await bus.publish(message)
        publish_ms = _elapsed_ms(publish_started)
        pipeline_duration_ms = _elapsed_ms(pipeline_started)
        logger.info(
            "decision_created",
            decision_id=decision_id,
            action=decision["action"],
            reason=decision["reason"],
            confidence=confidence,
            needs_ocr=False,
            decision_write_ms=decision_write_ms,
        )
        logger.info("decision_published", decision_id=decision_id, action=decision["action"], publish_ms=publish_ms)
        logger.info("pipeline_timing", cache_hit=False, paused=True, pipeline_duration_ms=pipeline_duration_ms, db_event_write_ms=db_event_write_ms, pause_check_ms=pause_check_ms, decision_write_ms=decision_write_ms, publish_ms=publish_ms)
        return message

    profile_started = time.perf_counter()
    child_id = event.get("child_id")
    profile = db.get_child_profile(child_id) if child_id else None
    if not profile and child_id:
        db.add_child_profile(child_id)
        profile = db.get_child_profile(child_id)
    if not profile:
        profile = {"id": child_id or "child_default", "strictness": "standard", "age": 12}
    profile_load_ms = _elapsed_ms(profile_started)

    _schedule_screenshot_save(str(event_id), event)
    log_step("event_received", event, {"upgrade": upgrade})

    normalized_url = ""
    cache_key = ""
    if settings.url_decision_cache_enabled and event.get("url"):
        normalized_url, cache_key = url_cache_key(
            url=event.get("url"),
            child_id=event.get("child_id"),
            strictness=profile.get("strictness"),
            age=profile.get("age"),
            policy_version=settings.policy_version,
        )

    if settings.url_decision_cache_enabled and not upgrade and cache_key:
        cache_started = time.perf_counter()
        cached = db.get_cached_url_decision(cache_key, settings.url_decision_cache_ttl_seconds)
        cache_lookup_ms = _elapsed_ms(cache_started)
        if cached:
            details = dict(cached.get("details_json") or {})
            confidence = float(details.get("confidence", 1.0) or 1.0)
            decision = {
                "action": cached.get("action"),
                "reason": cached.get("reason") or "url_cache",
                "categories": details.get("categories", []),
            }
            if details.get("rationale"):
                decision["llm_rationale"] = details.get("rationale")
            cached_details = {
                **details,
                "cache_hit": True,
                "cache_key": cache_key,
                "normalized_url": normalized_url,
                "cache_source": cached.get("source"),
                "source_decision_id": cached.get("source_decision_id"),
            }
            db.add_analysis(
                event_id,
                "url_decision_cache",
                "1.0",
                cached_details,
                label=decision["action"],
                latency_ms=int(cache_lookup_ms),
            )
            decision_write_started = time.perf_counter()
            decision_id = db.add_decision(
                event_id,
                settings.policy_version,
                decision["action"],
                decision["reason"],
                cached_details,
            )
            decision_write_ms = _elapsed_ms(decision_write_started)
            message = _format_decision_message(
                decision_id,
                event,
                decision,
                confidence=confidence,
                need_screenshot=False,
                headline_result=None,
                llm_rationale=details.get("rationale"),
            )
            publish_started = time.perf_counter()
            await bus.publish(message)
            publish_ms = _elapsed_ms(publish_started)
            pipeline_duration_ms = _elapsed_ms(pipeline_started)
            logger.info(
                "url_decision_cache_hit",
                cache_key=cache_key,
                normalized_url=normalized_url,
                source_decision_id=cached.get("source_decision_id"),
                action=decision["action"],
                cache_lookup_ms=cache_lookup_ms,
            )
            logger.info(
                "pipeline_timing",
                cache_hit=True,
                paused=False,
                pipeline_duration_ms=pipeline_duration_ms,
                db_event_write_ms=db_event_write_ms,
                pause_check_ms=pause_check_ms,
                profile_load_ms=profile_load_ms,
                cache_lookup_ms=cache_lookup_ms,
                decision_write_ms=decision_write_ms,
                publish_ms=publish_ms,
            )
            return message
        logger.info("url_decision_cache_miss", cache_key=cache_key, normalized_url=normalized_url, cache_lookup_ms=cache_lookup_ms)
    else:
        cache_lookup_ms = 0.0

    state = MonitorState(event=event, child_profile=profile, is_upgrade=upgrade)
    graph_started = time.perf_counter()
    logger.info("agent_graph_started", strictness=profile.get("strictness"), age=profile.get("age"))
    state = MonitorState(**app_graph.invoke(state))
    graph_duration_ms = _elapsed_ms(graph_started)
    logger.info(
        "agent_graph_finished",
        needs_ocr=state.needs_screenshot,
        has_judge=bool(state.judge_json),
        has_headline=bool(state.headline_result),
        graph_duration_ms=graph_duration_ms,
    )

    analysis_started = time.perf_counter()
    db.add_analysis(event_id, "fast+ocr", "1.0", state.fast_scores, label="")
    if state.judge_json:
        db.add_analysis(event_id, "llm_judge", "1.0", state.judge_json, label=state.judge_json.get("action",""))
    if state.headline_result:
        db.add_analysis(event_id, "headline_agent", "1.0", state.headline_result, label=state.headline_result.get("risk",""))
    analysis_write_ms = _elapsed_ms(analysis_started)

    confidence = state.judge_json.get("confidence", 1.0) if state.judge_json else 1.0
    need_screenshot = settings.enable_ocr and not upgrade and state.needs_screenshot
    if need_screenshot:
        logger.info("ocr_requested", confidence=confidence, threshold=settings.ocr_confidence_threshold)

    policy_started = time.perf_counter()
    if state.final_decision:
        decision = state.final_decision
    elif need_screenshot:
        decision = {"action": "warn", "reason": "pending_ocr", "categories": []}
    else:
        decision = policy.decide(event, state.fast_scores, state.judge_json, profile, state.headline_result)
    policy_duration_ms = _elapsed_ms(policy_started)

    llm_rationale = decision.get("llm_rationale") or (state.judge_json or {}).get("rationale")
    if llm_rationale:
        decision["llm_rationale"] = llm_rationale

    decision_details = {
        "categories": decision.get("categories", []),
        "confidence": confidence,
        **({"rationale": llm_rationale} if llm_rationale else {}),
    }
    decision_write_started = time.perf_counter()
    decision_id = db.add_decision(
        event_id,
        settings.policy_version,
        decision["action"],
        decision["reason"],
        decision_details,
    )
    decision_write_ms = _elapsed_ms(decision_write_started)
    logger.info(
        "decision_created",
        decision_id=decision_id,
        action=decision["action"],
        reason=decision["reason"],
        confidence=confidence,
        needs_ocr=need_screenshot,
        decision_write_ms=decision_write_ms,
    )

    cache_write_ms = 0.0
    should_cache_decision = (
        settings.url_decision_cache_enabled
        and cache_key
        and decision.get("reason") != "pending_ocr"
        and not need_screenshot
        and confidence >= settings.url_decision_cache_min_confidence
    )
    if should_cache_decision:
        cache_write_started = time.perf_counter()
        db.upsert_url_decision_cache(
            cache_key=cache_key,
            normalized_url=normalized_url,
            child_id=event.get("child_id"),
            strictness=profile.get("strictness"),
            age=profile.get("age"),
            policy_version=settings.policy_version,
            action=decision["action"],
            reason=decision["reason"],
            details=decision_details,
            source_decision_id=decision_id,
            source="pipeline",
        )
        cache_write_ms = _elapsed_ms(cache_write_started)
        logger.info(
            "url_decision_cache_stored",
            cache_key=cache_key,
            normalized_url=normalized_url,
            decision_id=decision_id,
            action=decision["action"],
            confidence=confidence,
            cache_write_ms=cache_write_ms,
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
    publish_started = time.perf_counter()
    await bus.publish(message)
    publish_ms = _elapsed_ms(publish_started)
    pipeline_duration_ms = _elapsed_ms(pipeline_started)
    logger.info("decision_published", decision_id=decision_id, action=decision["action"], publish_ms=publish_ms)
    logger.info(
        "pipeline_timing",
        cache_hit=False,
        cache_stored=should_cache_decision,
        paused=False,
        pipeline_duration_ms=pipeline_duration_ms,
        db_event_write_ms=db_event_write_ms,
        pause_check_ms=pause_check_ms,
        profile_load_ms=profile_load_ms,
        cache_lookup_ms=cache_lookup_ms,
        graph_duration_ms=graph_duration_ms,
        analysis_write_ms=analysis_write_ms,
        policy_duration_ms=policy_duration_ms,
        decision_write_ms=decision_write_ms,
        cache_write_ms=cache_write_ms,
        publish_ms=publish_ms,
    )
    return message
