from __future__ import annotations
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
# from langchain_community.chat_models import ChatOllama
from langchain_ollama import ChatOllama
from langchain.schema import SystemMessage, HumanMessage
from core.config import settings
from core.db import db
from core import activity_logger
import logging
import re
import json

logging.basicConfig(
    level=logging.DEBUG,  # or INFO if you want less
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
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
        self.client = ChatOllama(
            model=self.model,
            base_url=base_url or settings.ollama_base_url,
            temperature=0,
        )
        self.logger = logging.getLogger("watchit.llm")
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
            resp = self.client.invoke(msgs)
            raw = resp.content.strip()
            self.logger.debug("Raw LLM response: %s", raw)
            # Also capture raw responses in the session log for audit/debug.
            activity_logger.log_service_event(
                "llm_raw_response",
                {"model": self.model, "raw": raw[:2000]},
            )
        except Exception as e:
            self.logger.exception("Error calling Ollama LLM")
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
            self.logger.warning("JSON parse failed: %s. Raw: %s", e, raw)
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception as inner_e:
                    self.logger.error("Fallback JSON parse failed: %s", inner_e)
                    return fallback_allow
            else:
                self.logger.error("No JSON object found in response")
                return fallback_allow

        # Validate with Pydantic
        try:
            result = JudgeOut(**data).model_dump()
            return result
        except Exception as e:
            self.logger.error("Validation failed: %s. Data: %s", e, data)
            return fallback_allow
