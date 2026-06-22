from __future__ import annotations
import base64
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

# Docling may download model artifacts through its underlying ML stack. Keep
# those caches project-local so sandboxed/deployed workers do not require a
# writable user home.
cache_dir = Path(__file__).resolve().parents[4] / ".docling_cache"
cache_dir.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("DOCLING_CACHE_DIR", str(cache_dir))
os.environ.setdefault("HF_HOME", str(cache_dir / "huggingface"))
os.environ.setdefault("TORCH_HOME", str(cache_dir / "torch"))

_CONVERTER: Optional[Any] = None


def _get_converter() -> Any:
    global _CONVERTER
    if _CONVERTER is None:
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as exc:
            raise RuntimeError(
                "Docling is not installed. Install project dependencies or set WATCHIT_ENABLE_OCR=false."
            ) from exc
        _CONVERTER = DocumentConverter()
    return _CONVERTER


def _decode_image_payload(payload: str) -> tuple[bytes, str]:
    data = payload.strip()
    suffix = ".png"
    if data.startswith("data:") and "," in data:
        header, data = data.split(",", 1)
        if "jpeg" in header or "jpg" in header:
            suffix = ".jpg"
        elif "webp" in header:
            suffix = ".webp"
        elif "tiff" in header:
            suffix = ".tiff"
    return base64.b64decode(data), suffix


def _extract_text(document: Any) -> str:
    for method_name in ("export_to_text", "export_to_markdown"):
        method = getattr(document, method_name, None)
        if callable(method):
            text = method()
            if text:
                return _normalize_text(str(text))
    return _normalize_text(str(document) if document is not None else "")


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def ocr_image_b64(b64: str) -> str:
    raw, suffix = _decode_image_payload(b64)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)
        result = _get_converter().convert(tmp_path)
        return _extract_text(getattr(result, "document", result))
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
