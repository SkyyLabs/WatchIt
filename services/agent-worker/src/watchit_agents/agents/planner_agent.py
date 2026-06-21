from __future__ import annotations

import json
from typing import Any, Dict

from langchain_ollama import ChatOllama
from langchain.schema import SystemMessage, HumanMessage

from watchit_core.config import settings
from watchit_core.activity_logger import log_agent_step


class PlannerAgent:
    """Planner agent that chooses the next tool to run based on current state."""

    def __init__(self):
        self.client = ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=0,
        )

    def run(self, state: Any, child_profile: Dict[str, Any]) -> Dict[str, Any]:
        input_state = {
            "last_tool_run": state.last_tool_run,
            "need_llm": state.need_llm,
            "need_ocr": state.need_ocr,
            "needs_screenshot": state.needs_screenshot,
            "confidence": state.confidence,
            "fast_scores": state.fast_scores,
            "ocr_text_present": bool(state.ocr_text),
            "judge_json_present": bool(state.judge_json),
            "loop_count": state.loop_count,
            "has_ocr_run": state.has_ocr_run,
            "is_upgrade": state.is_upgrade,
        }
        system_prompt = (
            "You are a planner for a safety monitoring agent. "
            "Pick the next tool: headline, url_llm, ocr, policy, or stop. "
            "Stop when decision is ready; choose policy to finalize. "
            "Respond JSON: {\"next_tool\": \"headline|url_llm|ocr|policy|stop\", \"reason\": \"...\"}."
        )
        human_prompt = (
            f"State: {json.dumps(input_state, default=str)}\n"
            f"Child profile: {json.dumps(child_profile or {}, default=str)}"
        )
        # Default fallback
        next_tool = "policy"
        reason = "planner_fallback"
        if getattr(state, "has_ocr_run", False):
            log_agent_step(
                "PlannerAgent",
                "candidate_filter",
                getattr(state, "event", {}),
                {"has_ocr_run": True},
                {"filtered": ["ocr", "headline"]},
                {"loop_count": state.loop_count},
                "Filtered out headline/ocr due to has_ocr_run=True",
            )
        try:
            # If upgrade and OCR not yet run, force OCR first and skip headline entirely.
            if state.is_upgrade and not state.has_ocr_run:
                next_tool = "ocr"
                reason = "upgrade_prefers_ocr_first"
            else:
                resp = self.client.invoke([SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)])
                raw = (resp.content or "").strip()
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("planner non-dict")
                next_tool = data.get("next_tool") or "policy"
                reason = data.get("reason") or "planner_decision"
        except Exception:
            next_tool = "policy"
            reason = "planner_fallback"

        # Enforce constraints after OCR has run: remove headline/ocr
        if state.has_ocr_run and next_tool in {"ocr", "headline"}:
            log_agent_step(
                "PlannerAgent",
                "override",
                getattr(state, "event", {}),
                {"requested": next_tool},
                {"next_tool": "url_llm"},
                {"loop_count": state.loop_count, "has_ocr_run": state.has_ocr_run},
                "Prevented OCR/headline rerun after OCR; rerouted to URL agent",
            )
            next_tool = "url_llm"
        # If upgrade and planner tried headline, force ocr (pre-OCR) or url_llm (post-OCR)
        if state.is_upgrade and next_tool == "headline":
            next_tool = "ocr" if not state.has_ocr_run else "url_llm"
            reason = "upgrade_no_headline"

        output = {"next_tool": next_tool, "reason": reason}
        log_agent_step(
            "PlannerAgent",
            "plan",
            getattr(state, "event", {}),
            input_state,
            output,
            {"loop_count": state.loop_count, "needs_screenshot": state.needs_screenshot},
            f"planner selected {next_tool}",
        )
        return output
