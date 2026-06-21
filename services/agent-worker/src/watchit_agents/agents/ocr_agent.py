from __future__ import annotations

from typing import Dict, List, Any
import json

from watchit_agents.ocr_asr import ocr_image_b64


class ScreenshotsAgent:
    """Utility agent that knows how to inspect event payloads for screenshots."""

    def get_screenshots(self, event: Dict[str, Any]) -> List[str]:
        data = {}
        if event.get("data_json"):
            try:
                data = json.loads(event["data_json"]) or {}
            except Exception:
                data = {}
        shots = data.get("screenshots_b64") or []
        return [s for s in shots if isinstance(s, str)]


class OCRAgent:
    """Runs OCR on screenshots when instructed."""

    def __init__(self, limit: int = 3):
        self.limit = limit

    def extract_text(self, screenshots: List[str]) -> str:
        chunks: List[str] = []
        for b64 in screenshots[: self.limit]:
            try:
                text = ocr_image_b64(b64)
            except Exception:
                text = ""
            if text:
                chunks.append(text)
        return " ".join(chunks).strip()
