from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List
import re

from langchain.schema import HumanMessage, SystemMessage

from watchit_core.config import settings
from watchit_core.db import db
from watchit_core import activity_logger
from watchit_core.logging import bind_log_context, get_logger
from watchit_agents.llm_provider import build_chat_model


class GuardianLearningLoop:
    """Background job that analyzes manual overrides and distills guardian intent."""

    def __init__(self, interval_seconds: float = 3600.0):
        self.interval = interval_seconds
        self.logger = get_logger("watchit.guardian_learning")
        self.client = build_chat_model(temperature=0)

    async def run_forever(self) -> None:
        bind_log_context(service="learning-worker")
        db.connect()
        self.logger.info("guardian_learning_started", interval_seconds=self.interval)
        while True:
            try:
                await self.process_once()
            except asyncio.CancelledError:
                self.logger.info("guardian_learning_cancelled")
                raise
            except Exception:
                self.logger.exception("guardian_learning_loop_failed")
            await asyncio.sleep(self.interval)

    async def process_once(self) -> None:
        overrides = db.fetch_unprocessed_overrides(100)
        if not overrides:
            return
        self.logger.info("guardian_learning_started", override_count=len(overrides))
        guidance = await asyncio.to_thread(self._infer_guidance, overrides)

        # Merge with any existing guidance to avoid discarding past insights.
        merged_guidance, merged_patterns = self._merge_with_existing(guidance)

        payload = {
            "generated_at": int(time.time()),
            "guidance": merged_guidance,
            "patterns": merged_patterns,
            "sample_count": len(overrides),
        }
        household_id = overrides[0].get("household_id") or "hh_legacy"
        db.set_setting("guardian_feedback", json.dumps(payload), household_id)
        db.mark_override_processed([ov["override_id"] for ov in overrides if ov.get("override_id")])
        self.logger.info("guardian_feedback_updated", override_count=len(overrides), pattern_count=len(merged_patterns))
        # Mirror the guardian guidance update into the session log for traceability.
        activity_logger.log_service_event(
            "guardian_feedback_updated",
            {
                "sample_count": len(overrides),
                "guidance_preview": (merged_guidance or "")[:500],
                "patterns": merged_patterns[:5],
            },
        )
        # Capture the full stored guidance (trimmed by the session logger if huge).
        activity_logger.log_service_event(
            "guardian_feedback_value",
            {
                "guidance": merged_guidance,
                "patterns": merged_patterns,
                "sample_count": len(overrides),
            },
        )

    def _infer_guidance(self, overrides: List[Dict[str, Any]]) -> Dict[str, Any]:
        sample_lines = []
        for o in overrides[:15]:
            sample_lines.append(
                f"- URL:{o.get('url')} title:{o.get('title')} original:{o.get('original_action')} manual:{o.get('manual_action') or o.get('action')}"
            )
        prompt = "\n".join(sample_lines) or "No overrides."
        messages = [
            SystemMessage(
                content=(
                    "You review guardian overrides of a parental-control system. "
                    "Infer likely reasons (maturity, educational purpose, harmless fun, etc.) why a guardian corrected decisions."
                    "Respond in JSON with keys 'guidance' (short paragraph) and 'patterns' (array of short bullet strings)."
                )
            ),
            HumanMessage(
                content=(
                    "Recent overrides (each line: url/title/original->manual action):\n"
                    f"{prompt}\n\nSummarize motivations so the model can improve future moderation."
                )
            ),
        ]
        try:
            resp = self.client.invoke(messages)
            raw = resp.content.strip()
        except Exception as exc:
            self.logger.exception("guardian_insight_model_failed")
            return {"guidance": f"LLM feedback unavailable: {exc}", "patterns": []}
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            self.logger.warning("guardian_feedback_json_parse_failed")
        return {"guidance": raw, "patterns": []}

    def _merge_with_existing(self, new_guidance: Dict[str, Any]) -> tuple[str, List[str]]:
        """
        Merge newly inferred guidance with any existing stored guidance.
        - Guidance text: combine old + new, deduplicate similar sentences, keep concise.
        - Patterns: set-union of old and new, removing duplicates.
        """
        raw_existing = db.get_setting("guardian_feedback")
        existing_guidance = ""
        existing_patterns: List[str] = []
        if raw_existing:
            try:
                parsed = json.loads(raw_existing)
                if isinstance(parsed, dict):
                    existing_guidance = parsed.get("guidance") or ""
                    existing_patterns = parsed.get("patterns") or []
            except Exception:
                pass

        new_text = new_guidance.get("guidance") if isinstance(new_guidance, dict) else (new_guidance or "")
        new_patterns = new_guidance.get("patterns") if isinstance(new_guidance, dict) else []

        def dedup_sentences(text: str) -> List[str]:
            parts = [p.strip() for p in re.split(r"[\\.?!]+", text or "") if p.strip()]
            seen = set()
            result = []
            for p in parts:
                if p.lower() in seen:
                    continue
                seen.add(p.lower())
                result.append(p)
            return result

        existing_sents = dedup_sentences(existing_guidance)
        new_sents = dedup_sentences(new_text)
        merged_sents = []
        seen = set()
        for s in existing_sents + new_sents:
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            merged_sents.append(s)
        merged_guidance = ". ".join(merged_sents).strip()
        if merged_guidance and not merged_guidance.endswith("."):
            merged_guidance += "."

        merged_patterns = []
        seen_patterns = set()
        for p in (existing_patterns or []) + (new_patterns or []):
            if not isinstance(p, str):
                continue
            key = p.strip()
            if not key or key.lower() in seen_patterns:
                continue
            seen_patterns.add(key.lower())
            merged_patterns.append(key)

        return merged_guidance or (new_text or existing_guidance), merged_patterns
