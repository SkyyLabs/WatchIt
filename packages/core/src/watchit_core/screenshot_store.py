from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from datetime import datetime
from typing import Iterable, Mapping, Any

from watchit_core.config import settings
from watchit_core.logging import get_logger

logger = get_logger("watchit.screenshot_store")
_BASE_DIR = Path(__file__).resolve().parents[4]


def _resolve_dir() -> Path:
    raw = Path(settings.screenshots_dir).expanduser()
    path = raw if raw.is_absolute() else _BASE_DIR / raw
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_batch(event_id: str, screenshots: Iterable[str], metadata: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    date_part = datetime.now().strftime("%Y-%m-%d")
    base_dir = _resolve_dir() / date_part / str(event_id)
    base_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    for idx, b64 in enumerate(screenshots, start=1):
        try:
            blob = base64.b64decode(b64)
        except Exception:
            logger.warning("screenshot_payload_invalid", event_id=event_id, index=idx)
            continue
        target = base_dir / f"{idx:02d}.png"
        target.write_bytes(blob)
        saved.append(
            {
                "local_path": str(target),
                "sha256": hashlib.sha256(blob).hexdigest(),
                "size_bytes": len(blob),
            }
        )
    if metadata:
        meta_path = base_dir / "metadata.json"
        try:
            meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("screenshot_metadata_write_failed", event_id=event_id)
    return saved


async def persist_screenshots_async(event_id: str, screenshots: Iterable[str], metadata: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_save_batch, event_id, list(screenshots), metadata or {})
