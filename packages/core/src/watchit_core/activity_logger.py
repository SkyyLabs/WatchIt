from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from watchit_core.config import settings

# Base log directory; session-specific files are nested under logs/sessions/.
REPO_ROOT = Path(__file__).resolve().parents[4]
BASE_LOG_DIR = REPO_ROOT / "logs"
SESSION_DIR = BASE_LOG_DIR / "sessions"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

_logger: logging.Logger | None = None
_session_id: int | None = None


def _next_session_path() -> Path:
    """
    Compute the next session log path based on existing session files.
    Naming: YYYYMMDD_session_###.log under logs/sessions/.
    """
    today = datetime.now().strftime("%Y%m%d")
    pattern = re.compile(rf"{today}_session_(\d+)\.log")
    existing = sorted(SESSION_DIR.glob(f"{today}_session_*.log"))
    next_idx = 1
    for path in existing:
        m = pattern.match(path.name)
        if m:
            next_idx = max(next_idx, int(m.group(1)) + 1)
    return SESSION_DIR / f"{today}_session_{next_idx}.log"


def _get_logger() -> logging.Logger:
    """
    Create or reuse the session logger. On first use, allocate a new session file
    and store the session number for structured logs.
    """
    global _logger, _session_id
    if not settings.agent_trace_files:
        return logging.getLogger("watchit.activity.disabled")
    if _logger is not None:
        return _logger
    log_path = _next_session_path()
    try:
        # Extract session number for downstream structured logging
        m = re.match(r".*_session_(\d+)\.log", log_path.name)
        _session_id = int(m.group(1)) if m else 0
    except Exception:
        _session_id = 0
    logger = logging.getLogger("watchit.activity")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    _logger = logger
    logger.info("=== New WatchIt session log started: %s ===", log_path.name)
    return logger


def _safe_json(payload: Dict[str, Any]) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, default=str, indent=2)
    except Exception:
        return str(payload)


def log_step(step: str, event: Dict[str, Any], details: Dict[str, Any] | None = None) -> None:
    if not settings.agent_trace_files:
        return
    logger = _get_logger()
    url = event.get("url") or ""
    title = event.get("title") or ""
    payload = details or {}
    extra = _safe_json(payload)
    logger.info("step=%s\nurl=%s\ntitle=%s\nDETAILS:\n%s\n", step, url, title, extra[:4000])
    if step == "decision_finalized":
        logger.info("-------")


def log_service_event(event: str, details: Dict[str, Any] | None = None) -> None:
    if not settings.agent_trace_files:
        return
    logger = _get_logger()
    payload = details or {}
    extra = _safe_json(payload)
    logger.info("service=%s\nDETAILS:\n%s\n", event, extra[:4000])


def log_service_shutdown(details: Dict[str, Any] | None = None) -> None:
    if not settings.agent_trace_files:
        return
    logger = _get_logger()
    payload = details or {}
    extra = _safe_json(payload)
    logger.info("=== Service shutdown ===\nDETAILS:\n%s\n", extra[:4000])


def log_agent_step(agent_name: str, step: str, event: Dict[str, Any], input_dict: Dict[str, Any], output_dict: Dict[str, Any], state: Dict[str, Any], message: str = "") -> None:
    """
    Structured agent trace. Failures in logging must never break the pipeline.
    Fields:
      timestamp, session, agent, event_id, step, input, output, state_summary, message.
    """
    try:
        if not settings.agent_trace_files:
            return
        logger = _get_logger()
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "session": _session_id,
            "agent": agent_name,
            "event_id": event.get("id") or event.get("event_id"),
            "step": step,
            "input": input_dict,
            "output": output_dict,
            "state_summary": state,
            "message": message,
        }
        # Truncate potentially large fields
        def _truncate(val: Any):
            if isinstance(val, str) and len(val) > 120:
                return val[:120] + "..."
            return val
        entry["input"] = {k: _truncate(v) for k, v in (entry.get("input") or {}).items()}
        entry["output"] = {k: _truncate(v) for k, v in (entry.get("output") or {}).items()}
        entry["state_summary"] = {k: _truncate(v) for k, v in (entry.get("state_summary") or {}).items()}
        logger.info(json.dumps(entry, ensure_ascii=False, default=str, indent=2))
    except Exception:
        # Swallow logging errors to preserve pipeline execution.
        pass
