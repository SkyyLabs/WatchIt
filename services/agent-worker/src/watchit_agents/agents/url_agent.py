from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict
import json

from watchit_agents.safety import SafetyAnalyzer
from watchit_agents.llm_judge import LLMJudge


@dataclass
class URLAgentResult:
    fast_scores: Dict[str, float]
    llm_decision: Dict[str, Any]
    confidence: float


class URLMetadataAgent:
    """Primary agent that inspects URL metadata + DOM text and queries the LLM judge."""

    def __init__(self):
        self.analyzer = SafetyAnalyzer()
        self.judge = LLMJudge()

    def run(
        self,
        event: Dict[str, Any],
        child_profile: Dict[str, Any],
        extra_text: str = "",
        fast_scores: Dict[str, float] | None = None,
    ) -> URLAgentResult:
        if fast_scores is None:
            fast_scores = self.analyzer.analyze_event_fast(event, extra_text=extra_text)
        title = event.get("title") or ""
        url = event.get("url") or ""
        domain = url.split("//")[-1].split("/")[0] if url else ""
        child_age = int(child_profile.get("age", 12) or 12)
        strictness = (child_profile.get("strictness") or "standard").lower()
        llm_decision = self.judge.judge(
            page_title=title,
            domain=domain,
            fast_scores=fast_scores,
            text_sample=self._aggregate_text(event, extra_text),
            child_age=child_age,
            strictness=strictness,
        )
        confidence = float(llm_decision.get("confidence", 0.5))
        return URLAgentResult(
            fast_scores=fast_scores,
            llm_decision=llm_decision,
            confidence=max(0.0, min(1.0, confidence)),
        )

    def _aggregate_text(self, event: Dict[str, Any], extra_text: str) -> str:
        data_json = {}
        if event.get("data_json"):
            try:
                data_json = json.loads(event["data_json"]) or {}
            except Exception:
                data_json = {}
        text_parts = [
            data_json.get("dom_sample") or "",
            data_json.get("text") or "",
            extra_text or "",
        ]
        return "\n".join(part for part in text_parts if part).strip()
