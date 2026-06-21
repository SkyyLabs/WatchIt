from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List
from urllib.parse import urlparse

from watchit_agents.safety import SafetyAnalyzer

HIGH_RISK_TOKENS = ["porn", "xxx", "casino", "bet", "nsfw", "escort"]
LOW_RISK_DOMAINS = ["wikipedia.org", "khanacademy.org", ".edu"]


@dataclass
class HeadlinesAgentResult:
    risk: str
    flags: List[str]
    confidence: float
    action: str
    fast_scores: Dict[str, float]


class HeadlinesAgent:
    """Fast layer: cheap heuristics over title/domain + keyword scores."""

    def __init__(self):
        self.analyzer = SafetyAnalyzer()

    def run(
        self,
        event: Dict[str, Any],
        child_profile: Dict[str, Any],
    ) -> HeadlinesAgentResult:
        title = (event.get("title") or "").lower()
        url = event.get("url") or ""
        domain = (urlparse(url).netloc or "").lower()
        flags: List[str] = []
        fast_scores = self.analyzer.analyze_event_fast(event)

        sexual = fast_scores.get("sexual", 0.0)
        violence = fast_scores.get("violence", 0.0)
        profanity = fast_scores.get("profanity", 0.0)

        risk = "low"
        action = "allow"
        confidence = 0.5

        if any(token in domain or token in title for token in HIGH_RISK_TOKENS) or sexual >= 0.9 or violence >= 0.95:
            risk = "high"
            action = "block"
            flags.append("headline_high_risk")
            confidence = 0.9
        elif sexual < 0.15 and violence < 0.2 and profanity < 0.2 and any(dom in domain for dom in LOW_RISK_DOMAINS):
            risk = "low"
            action = "allow"
            flags.append("headline_low_risk")
            confidence = 0.88
        else:
            risk = "medium" if sexual > 0.4 or violence > 0.4 else "low"
            action = "allow"
            flags.append("headline_uncertain")
            confidence = 0.55

        strictness = (child_profile.get("strictness") or "standard").lower()
        if action == "block" and strictness == "strict":
            confidence = min(1.0, confidence + 0.05)
        if action == "allow" and strictness == "strict":
            confidence = max(0.0, confidence - 0.05)

        return HeadlinesAgentResult(
            risk=risk,
            flags=flags,
            confidence=confidence,
            action=action,
            fast_scores=fast_scores,
        )
