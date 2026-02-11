from __future__ import annotations

from typing import Dict, Any
from langgraph.graph import StateGraph, START, END
from pydantic import BaseModel, Field

from analysis.agents import (
    URLMetadataAgent,
    HeadlinesAgent,
    OCRAgent,
    ScreenshotsAgent,
    PlannerAgent,
    PolicyAgent,
)
from core.config import settings
from core.activity_logger import log_agent_step, log_step

HEADLINE_DECISION_THRESHOLD = 0.85
# Limit planner cycles to reduce latency and avoid loop stalls.
MAX_LOOPS = 3


class MonitorState(BaseModel):
    event: Dict[str, Any]
    child_profile: Dict[str, Any] = Field(default_factory=dict)
    fast_scores: Dict[str, float] = Field(default_factory=dict)
    judge_json: Dict[str, Any] = Field(default_factory=dict)
    headline_result: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    ocr_text: str = ""
    need_llm: bool = True
    need_ocr: bool = False
    needs_screenshot: bool = False
    last_tool_run: str = ""
    next_tool: str = "planner"
    planner_reason: str = ""
    loop_count: int = 0
    final_decision: Dict[str, Any] = Field(default_factory=dict)
    has_ocr_run: bool = False
    is_upgrade: bool = False


headlines_agent = HeadlinesAgent()
url_agent = URLMetadataAgent()
ocr_agent = OCRAgent()
screens_agent = ScreenshotsAgent()
planner_agent = PlannerAgent()
policy_agent = PolicyAgent()


def node_planner(state: MonitorState) -> MonitorState:
    # Loop protection
    state.loop_count += 1
    if state.loop_count >= MAX_LOOPS:
        state.next_tool = "policy"
        state.planner_reason = "max_loops_reached"
        log_agent_step(
            "PlannerAgent",
            "plan_max_loops",
            state.event,
            {"loop_count": state.loop_count},
            {"next_tool": state.next_tool},
            {"loop_count": state.loop_count},
            "Loop protection triggered; routing to policy",
        )
        return state
    plan = planner_agent.run(state, state.child_profile)
    state.next_tool = plan.get("next_tool") or "policy"
    state.planner_reason = plan.get("reason") or ""
    log_agent_step(
        "PlannerAgent",
        "plan",
        state.event,
        {"loop_count": state.loop_count - 1},
        {"next_tool": state.next_tool, "reason": state.planner_reason},
        {"loop_count": state.loop_count},
        f"Planner chose {state.next_tool}",
    )
    return state


def node_headline_layer(state: MonitorState) -> MonitorState:
    result = headlines_agent.run(state.event, state.child_profile)
    state.last_tool_run = "headline"
    state.fast_scores = result.fast_scores
    state.headline_result = {
        "risk": result.risk,
        "flags": result.flags,
        "confidence": result.confidence,
        "action": result.action,
    }
    state.need_llm = True
    if result.action in ("allow", "block") and result.confidence >= HEADLINE_DECISION_THRESHOLD:
        state.need_llm = False
        state.judge_json = {
            "action": result.action,
            "categories": result.flags,
            "severity": "medium" if result.action == "block" else "low",
            "rationale": "headline_agent_decision",
            "confidence": result.confidence,
            "is_harmful": result.action != "allow",
        }
        state.confidence = result.confidence
    log_agent_step(
        "HeadlinesAgent",
        "headline_layer",
        state.event,
        {"child_profile": state.child_profile},
        state.headline_result,
        {"need_llm": state.need_llm, "loop_count": state.loop_count},
        "Headline agent evaluated headline/meta",
    )
    state.next_tool = "planner"
    return state


def node_url_layer(state: MonitorState) -> MonitorState:
    if not state.need_llm:
        state.next_tool = "planner"
        return state
    result = url_agent.run(
        state.event,
        state.child_profile,
        extra_text=state.ocr_text,
        fast_scores=state.fast_scores or None,
    )
    state.last_tool_run = "url_llm"
    state.fast_scores = result.fast_scores
    state.judge_json = result.llm_decision
    state.confidence = result.confidence
    llm_action = (result.llm_decision or {}).get("action", "").lower()
    llm_severity = (result.llm_decision or {}).get("severity", "").lower()
    uncertain = (
        result.confidence < settings.ocr_confidence_threshold
        or llm_action in {"warn", "blur", "notify"}
        or llm_severity in {"medium", "high"}
    )
    state.need_ocr = uncertain
    log_agent_step(
        "URLMetadataAgent",
        "url_layer",
        state.event,
        {
            "fast_scores": state.fast_scores,
            "ocr_text_preview": (state.ocr_text or "")[:120],
        },
        {
            "llm_decision": result.llm_decision,
            "confidence": result.confidence,
            "need_ocr": state.need_ocr,
        },
        {"loop_count": state.loop_count},
        "URL agent evaluated content",
    )
    state.next_tool = "planner"
    return state


def node_ocr_layer(state: MonitorState) -> MonitorState:
    state.last_tool_run = "ocr"
    state.needs_screenshot = False
    # Enforce single OCR run per event
    if state.has_ocr_run:
        state.next_tool = "planner"
        return state
    state.has_ocr_run = True

    screenshots = screens_agent.get_screenshots(state.event)
    if not screenshots:
        state.needs_screenshot = True
        log_agent_step(
            "OCRAgent",
            "ocr_request",
            state.event,
            {"needs_screenshot": True},
            {"screenshot_count": 0},
            {"loop_count": state.loop_count},
            "No screenshots present; requesting upgrade",
        )
        state.next_tool = "planner"
        return state

    ocr_text = ocr_agent.extract_text(screenshots)
    if not ocr_text:
        log_agent_step(
            "OCRAgent",
            "ocr_empty",
            state.event,
            {"screenshot_count": len(screenshots)},
            {"ocr_text": ""},
            {"loop_count": state.loop_count},
            "OCR returned empty text",
        )
        state.next_tool = "planner"
        return state

    state.ocr_text = ocr_text
    refreshed = url_agent.run(
        state.event,
        state.child_profile,
        extra_text=ocr_text,
        fast_scores=state.fast_scores or None,
    )
    state.fast_scores = refreshed.fast_scores
    state.judge_json = refreshed.llm_decision
    state.confidence = refreshed.confidence
    state.need_ocr = False
    log_agent_step(
        "OCRAgent",
        "ocr_run",
        state.event,
        {"screenshot_count": len(screenshots)},
        {
            "ocr_text_preview": ocr_text[:120],
            "llm_decision": refreshed.llm_decision,
            "confidence": refreshed.confidence,
        },
        {"loop_count": state.loop_count},
        "OCR executed for event. Future OCR attempts disabled.",
    )
    state.next_tool = "planner"
    return state


def node_policy_layer(state: MonitorState) -> MonitorState:
    decision = policy_agent.run(state, state.child_profile)
    state.final_decision = decision
    state.last_tool_run = "policy"
    state.next_tool = "stop"
    return state


graph = StateGraph(MonitorState)
graph.add_node("planner", node_planner)
graph.add_node("headline_layer", node_headline_layer)
graph.add_node("url_layer", node_url_layer)
graph.add_node("ocr_layer", node_ocr_layer)
graph.add_node("policy_layer", node_policy_layer)


def _route_from_planner(state: MonitorState) -> str:
    # Prevent headline/ocr after OCR has run
    if state.has_ocr_run and state.next_tool in {"ocr", "headline"}:
        log_agent_step(
            "PlannerAgent",
            "override",
            state.event,
            {"requested": state.next_tool},
            {"next_tool": "url_llm"},
            {"loop_count": state.loop_count, "has_ocr_run": state.has_ocr_run},
            "Prevented OCR/headline rerun after OCR; routing to URL agent",
        )
        return "url_llm"
    return state.next_tool or "policy"


graph.add_conditional_edges(
    "planner",
    _route_from_planner,
    {
        "headline": "headline_layer",
        "url_llm": "url_layer",
        "ocr": "ocr_layer",
        "policy": "policy_layer",
        "stop": END,
    },
)
# After any tool, return to planner unless policy/stop.
graph.add_edge("headline_layer", "planner")
graph.add_edge("url_layer", "planner")
graph.add_edge("ocr_layer", "planner")
graph.add_edge("policy_layer", END)
graph.add_edge(START, "planner")
app_graph = graph.compile()
