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
    "You are a child-safety content classifier for a parental monitoring system. "
    "Decide whether a web page is appropriate for a child aged {age} and choose an enforcement action.\n\n"
    "GUARDIAN STRICTNESS: {strictness}\n"
    "- lenient: block only clearly harmful or explicit content; allow borderline cases.\n"
    "- standard: block clear harm, caution on borderline content, allow benign pages.\n"
    "- strict: block anything doubtful; only allow content that is clearly age-appropriate.\n"
    "Younger ages lower the threshold for blocking.\n\n"
    "SIGNALS you may receive: URL/domain, page title and meta text, a visible-text sample, "
    "fast heuristic scores, and OCR text from screenshots. Weigh every signal that is present. "
    "A signal being absent means unknown, not safe.\n\n"
    "CATEGORIES — choose zero or more, only from this list: "
    "adult_sexual, violence_gore, hate_harassment, self_harm, drugs_alcohol, gambling, "
    "weapons, profanity, scam_malware, age_gate, other.\n\n"
    "ACTION — pick exactly one:\n"
    "- allow: age-appropriate; no concern.\n"
    "- notify: allowed, but worth a guardian heads-up.\n"
    "- warn: keep the page but show the child a caution.\n"
    "- blur: obscure the content while the child stays on the page.\n"
    "- block: prevent access entirely.\n"
    "Escalate toward block as severity rises and as strictness increases.\n\n"
    "severity: low | medium | high — the worst issue found (low when there is none).\n"
    "confidence: 0.0-1.0 — how sure you are the chosen action is correct.\n"
    "rationale: <=30 words, concrete and specific to this page; no boilerplate.\n\n"
    "Respond with ONE JSON object and nothing else — no markdown, no code fences, no preamble. "
    "Keys in this order: is_harmful (bool), categories (array of the labels above), "
    "severity (low|medium|high), rationale (string), action (allow|notify|warn|blur|block), "
    "confidence (number 0.0-1.0).\n"
    'Example: {{"is_harmful": true, "categories": ["adult_sexual"], "severity": "high", '
    '"rationale": "Explicit pornographic imagery and text throughout the page.", '
    '"action": "block", "confidence": 0.95}}'
)

def build_human_prompt(page_title: str, domain: str, fast_scores: Dict[str, float], text_sample: str, child_age: int, strictness: str) -> str:
    # Keep payload compact (cap text to ~2000 chars) and render scores as JSON so
    # the model reads clean key/value pairs rather than a Python dict repr.
    text_snippet = (text_sample or "")[:2000]
    scores = json.dumps(fast_scores or {}, sort_keys=True)
    return (
        f"CHILD_PROFILE: age={child_age}, strictness={strictness}\n"
        f"DOMAIN: {domain}\n"
        f"PAGE_TITLE: {page_title or '(none)'}\n"
        f"FAST_SCORES: {scores}\n"
        f"TEXT_SAMPLE (may be truncated):\n{text_snippet or '(none)'}\n\n"
        "Classify this page and reply with only the JSON object."
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
            self.logger.debug(
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
            # Privacy: never put raw model output in structlog — it can quote the
            # child's page content. Full text goes only to the flag-gated trace log.
            self.logger.debug("llm_response_received", raw_len=len(raw), llm_duration_ms=llm_duration_ms)
            # Also capture raw responses in the session log for audit/debug.
            activity_logger.log_service_event(
                "llm_raw_response",
                {"model": self.model, "raw": raw[:2000]},
            )
        except Exception as e:
            # Risk-tiered failure policy: a judge outage must never silently become
            # unrestricted browsing. warn ⇒ blur + banner; low confidence keeps it
            # out of the URL decision cache and escalates to OCR where enabled.
            self.logger.exception("llm_call_failed", provider=settings.llm_provider, model=self.model)
            return {
                "is_harmful": False,
                "categories": ["system_uncertain"],
                "severity": "medium",
                "rationale": f"LLM call failed: {e}",
                "action": "warn",
                "confidence": 0.0,
            }

        fallback_uncertain = {
            "is_harmful": False,
            "categories": ["system_uncertain", "llm_parse_error"],
            "severity": "medium",
            "rationale": "LLM output invalid; degrading to warn.",
            "action": "warn",
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
                    return fallback_uncertain
            else:
                self.logger.error("llm_json_object_missing")
                return fallback_uncertain

        # Validate with Pydantic
        try:
            result = JudgeOut(**data).model_dump()
            self.logger.debug(
                "llm_judge_finished",
                action=result.get("action"),
                confidence=result.get("confidence"),
                severity=result.get("severity"),
                llm_duration_ms=llm_duration_ms,
            )
            return result
        except Exception as e:
            self.logger.error("llm_validation_failed", error=str(e), data=data)
            return fallback_uncertain
