from __future__ import annotations
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from langchain.schema import SystemMessage, HumanMessage
from watchit_core.config import settings
from watchit_core.db import db
from watchit_core import activity_logger
from watchit_core.logging import get_logger
from watchit_agents.llm_provider import build_chat_model
import re
import json
import time

# Structured output schema
class JudgeOut(BaseModel):
    is_harmful: bool
    categories: List[str] = Field(default_factory=list)
    severity: str        # "low" | "medium" | "high"
    rationale: str       # <= 30 words
    action: str          # "allow" | "warn" | "blur" | "block" | "notify"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

SYSTEM_PROMPT_TEMPLATE = (
"You are a children's online safety classifier running on-device. "
"Evaluate if the given webpage content is suitable for a child age {age}. "
"Guardian strictness level is '{strictness}'. Lenient means only block severe harm, "
"standard balances caution, strict should warn or block whenever uncertain. "
"Use every signal provided: URL/domain (e.g., nsfw subdomains), page title/meta tags, "
"visible text, hyperlinks, scripts/trackers, and OCR summaries of screenshots (if present). "
"Flag issues such as adult content, gambling, hate, violence, drugs, self-harm, bullying, "
"or risky redirects hinted by the metadata. "
"Be conservative relative to the requested strictness. If unsure, choose 'warn'. "
"Output STRICT JSON with keys: is_harmful (bool), categories (array), "
"severity (low|medium|high), rationale (<=30 words), action (allow|warn|blur|block|notify), "
"confidence (0.0-1.0 expressing how certain you are in the requested action)."
)

def build_human_prompt(page_title: str, domain: str, fast_scores: Dict[str, float], text_sample: str, child_age: int, strictness: str) -> str:
    # Keep payload compact (cap text to ~2000 chars)
    text_snippet = (text_sample or "")[:2000]
    return (
        f"PAGE_TITLE: {page_title}\n"
        f"DOMAIN: {domain}\n"
        f"CHILD_PROFILE: age={child_age}, strictness={strictness}\n"
        f"FAST_SCORES: {fast_scores}\n"
        "RISK_HINTS: use URL keywords (nsfw, porn, casino), metadata text, hyperlinks, "
        "scripts/trackers, sentiment, OCR text for images/videos, and tone for slurs/bullying.\n"
        f"TEXT_SNIPPET:\n{text_snippet}\n\n"
        "Return STRICT JSON only."
    )

class LLMJudge:
    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None):
        self.model = model or settings.ollama_model
        self.client = build_chat_model(model=model, base_url=base_url, temperature=0)
        self.logger = get_logger("watchit.llm")
        self._guardian_cache: Optional[str] = None


    def _guardian_guidance(self) -> Optional[str]:
        raw = db.get_setting("guardian_feedback")
        if not raw:
            self._guardian_cache = None
            return None
        if raw == self._guardian_cache:
            try:
                data = json.loads(raw)
            except Exception:
                return raw
            guidance = data.get("guidance") or ""
            patterns = data.get("patterns") or []
            if patterns:
                guidance = guidance + "\nPatterns: " + "; ".join(patterns[:5])
            return guidance
        try:
            data = json.loads(raw)
        except Exception:
            self._guardian_cache = raw
            return raw
        self._guardian_cache = raw
        guidance = data.get("guidance") or ""
        patterns = data.get("patterns") or []
        if patterns:
            guidance = guidance + "\nPatterns: " + "; ".join(patterns[:5])
        return guidance or None

    
    def judge(
        self,
        page_title: str,
        domain: str,
        fast_scores: Dict[str, float],
        text_sample: str,
        child_age: int,
        strictness: str,
    ) -> Dict[str, Any]:
        if strictness not in {"lenient", "standard", "strict"}:
            strictness = "standard"
        try:
            child_age = int(child_age)
        except Exception:
            child_age = 12
        child_age = max(3, min(18, child_age))
        prompt = build_human_prompt(page_title, domain, fast_scores, text_sample, child_age, strictness)
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(age=child_age, strictness=strictness)
        guardian_guidance = self._guardian_guidance()
        if guardian_guidance:
            system_prompt += "\nGuardian feedback to prioritize:\n" + guardian_guidance
        msgs = [SystemMessage(content=system_prompt), HumanMessage(content=prompt)]

        # Send to Ollama
        try:
            llm_started = time.perf_counter()
            self.logger.info(
                "llm_judge_started",
                provider=settings.llm_provider,
                model=self.model,
                strictness=strictness,
                child_age=child_age,
                domain=domain,
            )
            resp = self.client.invoke(msgs)
            llm_duration_ms = round((time.perf_counter() - llm_started) * 1000, 2)
            raw = resp.content.strip()
            self.logger.debug("llm_raw_response", raw=raw[:2000], llm_duration_ms=llm_duration_ms)
            # Also capture raw responses in the session log for audit/debug.
            activity_logger.log_service_event(
                "llm_raw_response",
                {"model": self.model, "raw": raw[:2000]},
            )
        except Exception as e:
            self.logger.exception("llm_call_failed", provider=settings.llm_provider, model=self.model)
            return {
                "is_harmful": False,
                "categories": [],
                "severity": "low",
                "rationale": f"LLM call failed: {e}",
                "action": "allow",
                "confidence": 0.0,
            }

        fallback_allow = {
            "is_harmful": False,
            "categories": ["llm_parse_error"],
            "severity": "low",
            "rationale": "LLM output invalid; defaulting to allow.",
            "action": "allow",
            "confidence": 0.2,
        }

        # Try to parse JSON
        try:
            data = json.loads(raw)
        except Exception as e:
            self.logger.warning("llm_json_parse_failed", error=str(e), raw=raw[:1000])
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception as inner_e:
                    self.logger.error("llm_json_fallback_parse_failed", error=str(inner_e))
                    return fallback_allow
            else:
                self.logger.error("llm_json_object_missing")
                return fallback_allow

        # Validate with Pydantic
        try:
            result = JudgeOut(**data).model_dump()
            self.logger.info(
                "llm_judge_finished",
                action=result.get("action"),
                confidence=result.get("confidence"),
                severity=result.get("severity"),
                llm_duration_ms=llm_duration_ms,
            )
            return result
        except Exception as e:
            self.logger.error("llm_validation_failed", error=str(e), data=data)
            return fallback_allow
