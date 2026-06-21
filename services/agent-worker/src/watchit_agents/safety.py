from __future__ import annotations
import json, re
from typing import Dict, Any
class SafetyAnalyzer:
    def __init__(self):
        self.violence = ["kill","shoot","gun","fight","blood","weapon"]
        self.sexual   = ["sex","porn","nude","xxx","18+","adult only"]
        self.profanity= ["damn","shit","fuck","bitch"]
        self.re_v = re.compile(r"\b(" + "|".join(map(re.escape, self.violence)) + r")\b", re.I)
        self.re_s = re.compile(r"\b(" + "|".join(map(re.escape, self.sexual)) + r")\b", re.I)
        self.re_p = re.compile(r"\b(" + "|".join(map(re.escape, self.profanity)) + r")\b", re.I)

    def analyze_text(self, text: str) -> Dict[str, float]:
        if not text:
            return {"violence":0.0,"sexual":0.0,"profanity":0.0}
        words = max(1, len(text.split()))
        v = len(self.re_v.findall(text)) / words * 5.0
        s = len(self.re_s.findall(text)) / words * 5.0
        p = len(self.re_p.findall(text)) / words * 5.0
        return {"violence": round(min(1.0, v),3), "sexual": round(min(1.0, s),3), "profanity": round(min(1.0, p),3)}

    def analyze_event_fast(self, event: Dict[str, Any], extra_text: str = "") -> Dict[str, float]:
        text = ""
        data = {}
        if event.get("data_json"):
            try: data = json.loads(event["data_json"]) or {}
            except Exception: data = {}
        if data.get("dom_sample"): text += " " + data["dom_sample"]
        if data.get("text"):       text += " " + data["text"]
        if event.get("kind") == "search" and event.get("title"):
            text += " " + (event.get("title") or "")
        if extra_text:
            text += " " + extra_text

        return self.analyze_text(text)
