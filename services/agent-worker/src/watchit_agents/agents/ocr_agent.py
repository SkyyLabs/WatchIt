from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Dict, List, Any
import json

from watchit_agents.ocr_asr import ocr_image_b64
from watchit_core.logging import get_logger

logger = get_logger("watchit.ocr_agent")


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

    TIMEOUT_SECONDS = 20

    def __init__(self, limit: int = 3):
        self.limit = limit

    def extract_text(self, screenshots: List[str]) -> str:
        chunks: List[str] = []
        for b64 in screenshots[: self.limit]:
            text = ""
            # Docling can hang on a bad frame; a stuck OCR must not stall the
            # queue. shutdown(wait=False) leaves the stray thread to finish in
            # the background — the converter offers no cancellation.
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                text = pool.submit(ocr_image_b64, b64).result(timeout=self.TIMEOUT_SECONDS)
            except FutureTimeout:
                logger.warning("ocr_timeout", timeout_seconds=self.TIMEOUT_SECONDS, image_bytes=len(b64))
            except Exception:
                logger.warning("ocr_failed", image_bytes=len(b64), exc_info=True)
            finally:
                pool.shutdown(wait=False)
            if text:
                chunks.append(text)
        return " ".join(chunks).strip()
