from __future__ import annotations

from typing import Any, Dict
from langgraph.graph import StateGraph, START, END
from pydantic import BaseModel, Field

from watchit_agents.agents import (
    URLMetadataAgent,
    HeadlinesAgent,
    OCRAgent,
    ScreenshotsAgent,
    PolicyAgent,
)
from watchit_core.config import settings
from watchit_core.activity_logger import log_agent_step

# Headline is decisive only at high confidence: clearly unsafe (high-risk
# tokens/scores) -> block fast, clearly safe (curated allowlist) -> allow.
# Anything else is uncertain and MUST escalate to the LLM — never allow a page
# through on a weak cheap signal (false-negative guard).
HEADLINE_DECISION_THRESHOLD = 0.85


def _vision_judge_active() -> bool:
    # Screenshots go straight to the multimodal judge only when the provider
    # can actually see them; Docling OCR remains the local/ollama path.
    return settings.vision_judge and (settings.llm_provider or "").lower() in {"anthropic", "claude"}


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
    final_decision: Dict[str, Any] = Field(default_factory=dict)
    has_ocr_run: bool = False
    is_upgrade: bool = False


headlines_agent = HeadlinesAgent()
url_agent = URLMetadataAgent()
ocr_agent = OCRAgent()
screens_agent = ScreenshotsAgent()
policy_agent = PolicyAgent()


def node_headline(state: MonitorState) -> MonitorState:
    result = headlines_agent.run(state.event, state.child_profile)
    state.last_tool_run = "headline"
    state.fast_scores = result.fast_scores
    state.headline_result = {
        "risk": result.risk,
        "flags": result.flags,
        "confidence": result.confidence,
        "action": result.action,
    }
    # Only a high-confidence allow/block short-circuits the LLM.
    state.need_llm = not (
        result.action in ("allow", "block") and result.confidence >= HEADLINE_DECISION_THRESHOLD
    )
    if not state.need_llm:
        state.judge_json = {
            "action": result.action,
            "categories": result.flags,
            "severity": "high" if result.action == "block" else "low",
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
        {"need_llm": state.need_llm},
        "Headline agent evaluated headline/meta",
    )
    return state


def node_url_llm(state: MonitorState) -> MonitorState:
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
    # Clearly-harmful judgments block immediately — don't defer to OCR (avoids a
    # false-negative window while waiting for a screenshot).
    clearly_harmful = llm_action == "block" or llm_severity == "high"
    uncertain = (
        result.confidence < settings.ocr_confidence_threshold
        or llm_action in {"warn", "blur", "notify"}
        or llm_severity == "medium"
    )
    state.need_ocr = uncertain and not clearly_harmful
    log_agent_step(
        "URLMetadataAgent",
        "url_layer",
        state.event,
        {"fast_scores": state.fast_scores, "ocr_text_preview": (state.ocr_text or "")[:120]},
        {"llm_decision": result.llm_decision, "confidence": result.confidence, "need_ocr": state.need_ocr},
        {},
        "URL agent evaluated content",
    )
    return state


def node_ocr(state: MonitorState) -> MonitorState:
    state.last_tool_run = "ocr"
    if state.has_ocr_run:
        return state
    state.has_ocr_run = True

    screenshots = screens_agent.get_screenshots(state.event)
    if screenshots:
        vision = _vision_judge_active()
        if vision:
            # Multimodal path: the judge sees the screenshot directly — removes
            # the Docling hop and a second text-only round trip.
            ocr_text = ""
            refreshed = url_agent.run(
                state.event,
                state.child_profile,
                fast_scores=state.fast_scores or None,
                images_b64=screenshots,
            )
        else:
            ocr_text = ocr_agent.extract_text(screenshots)
            # Always re-judge (even when OCR text is empty) so judge_json is populated
            # and policy never falls through to its default-allow branch.
            refreshed = url_agent.run(
                state.event,
                state.child_profile,
                extra_text=ocr_text,
                fast_scores=state.fast_scores or None,
            )
        state.ocr_text = ocr_text
        state.fast_scores = refreshed.fast_scores
        state.judge_json = refreshed.llm_decision
        state.confidence = refreshed.confidence
        state.need_ocr = False
        state.needs_screenshot = False
        log_agent_step(
            "OCRAgent",
            "vision_judge_run" if vision else "ocr_run",
            state.event,
            {"screenshot_count": len(screenshots), "vision": vision},
            {"ocr_text_preview": ocr_text[:120], "llm_decision": refreshed.llm_decision, "confidence": refreshed.confidence},
            {},
            "screenshots judged directly" if vision else "OCR executed; re-judged with OCR text",
        )
        return state

    # No screenshots attached.
    if state.is_upgrade:
        # An upgrade already carried its screenshots; if none arrived, don't loop —
        # make sure we hold a judgment so policy doesn't default-allow.
        if not state.judge_json:
            refreshed = url_agent.run(
                state.event,
                state.child_profile,
                fast_scores=state.fast_scores or None,
            )
            state.judge_json = refreshed.llm_decision
            state.confidence = refreshed.confidence
        state.needs_screenshot = False
        return state

    # First pass: ask the extension for a screenshot. Runtime emits an interim
    # pending_ocr (warn — never allow) and waits for the /v1/event/upgrade re-run.
    state.needs_screenshot = True
    log_agent_step(
        "OCRAgent",
        "ocr_request",
        state.event,
        {"needs_screenshot": True},
        {"screenshot_count": 0},
        {},
        "No screenshots present; requesting upgrade",
    )
    return state


def node_policy(state: MonitorState) -> MonitorState:
    state.final_decision = policy_agent.run(state, state.child_profile)
    state.last_tool_run = "policy"
    return state


graph = StateGraph(MonitorState)
graph.add_node("headline", node_headline)
graph.add_node("url_llm", node_url_llm)
graph.add_node("ocr", node_ocr)
graph.add_node("policy", node_policy)


def _entry(state: MonitorState) -> str:
    # A screenshot upgrade carries its screenshots — go straight to OCR; otherwise
    # start with the cheap headline layer.
    return "ocr" if state.is_upgrade else "headline"


def _after_headline(state: MonitorState) -> str:
    return "policy" if not state.need_llm else "url_llm"


def _after_url(state: MonitorState) -> str:
    # Defer to OCR only when the judgment is ambiguous AND OCR is enabled. With
    # OCR off, decide now from the LLM judgment instead of stalling.
    if state.need_ocr and settings.enable_ocr:
        return "ocr"
    return "policy"


def _after_ocr(state: MonitorState) -> str:
    # Only end without a decision when we actually requested a screenshot; runtime
    # then emits the interim pending_ocr and waits for the upgrade.
    return "pending" if state.needs_screenshot else "policy"


graph.add_conditional_edges(START, _entry, {"headline": "headline", "ocr": "ocr"})
graph.add_conditional_edges("headline", _after_headline, {"policy": "policy", "url_llm": "url_llm"})
graph.add_conditional_edges("url_llm", _after_url, {"ocr": "ocr", "policy": "policy"})
graph.add_conditional_edges("ocr", _after_ocr, {"policy": "policy", "pending": END})
graph.add_edge("policy", END)
app_graph = graph.compile()
