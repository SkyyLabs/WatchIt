"""Vision judge: multimodal message shape, routing, OCR timeout, upload limits."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from watchit_agents import graph
from watchit_agents.agents.ocr_agent import OCRAgent
from watchit_agents.llm_judge import LLMJudge, _image_block
from watchit_api import main
from watchit_core.config import settings

VALID_JUDGMENT = json.dumps({
    "is_harmful": False, "categories": [], "severity": "low",
    "rationale": "ok", "action": "allow", "confidence": 0.9,
})


def _judge(captured):
    judge = LLMJudge.__new__(LLMJudge)  # skip __init__: no model client needed
    judge.model = "test-model"
    judge.logger = SimpleNamespace(
        debug=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, exception=lambda *a, **k: None,
    )
    def invoke(msgs):
        captured["msgs"] = msgs
        return SimpleNamespace(content=VALID_JUDGMENT)

    judge.client = SimpleNamespace(invoke=invoke)
    return judge


# --- multimodal message shape --------------------------------------------------

def test_image_block_sniffs_format():
    assert _image_block("/9j/abc")["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert _image_block("iVBORabc")["image_url"]["url"].startswith("data:image/png;base64,")
    assert _image_block("data:image/webp;base64,x")["image_url"]["url"] == "data:image/webp;base64,x"


def test_judge_with_images_builds_multimodal_message(monkeypatch):
    captured = {}
    judge = _judge(captured)
    monkeypatch.setattr(LLMJudge, "_guardian_guidance", lambda self: None)
    out = judge.judge(
        page_title="t", domain="example.com", fast_scores={}, text_sample="",
        child_age=12, strictness="standard", images_b64=["/9j/a", "/9j/b", "/9j/c", "/9j/d"],
    )
    assert out["action"] == "allow"
    human = captured["msgs"][1]
    assert isinstance(human.content, list)
    assert human.content[0]["type"] == "text"
    images = [c for c in human.content if c["type"] == "image_url"]
    assert len(images) == 3  # capped


def test_judge_without_images_stays_text_only(monkeypatch):
    captured = {}
    judge = _judge(captured)
    monkeypatch.setattr(LLMJudge, "_guardian_guidance", lambda self: None)
    judge.judge(page_title="t", domain="d", fast_scores={}, text_sample="", child_age=12, strictness="standard")
    assert isinstance(captured["msgs"][1].content, str)


# --- routing: vision vs docling ---------------------------------------------------

def _state(shots):
    return graph.MonitorState(
        event={"id": "evt_1", "url": "https://x.com", "data_json": json.dumps({"screenshots_b64": shots})},
        child_profile={"age": 12, "strictness": "standard"},
        is_upgrade=True,
    )


def _fake_result():
    return SimpleNamespace(fast_scores={}, llm_decision={"action": "allow", "confidence": 0.9}, confidence=0.9)


def test_vision_active_skips_docling(monkeypatch):
    monkeypatch.setattr(settings, "vision_judge", True)
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    seen = {}
    monkeypatch.setattr(graph.url_agent, "run", lambda *a, **k: seen.update(k) or _fake_result())
    monkeypatch.setattr(graph.ocr_agent, "extract_text", lambda shots: (_ for _ in ()).throw(AssertionError("docling must not run")))
    state = graph.node_ocr(_state(["/9j/a"]))
    assert seen["images_b64"] == ["/9j/a"]
    assert state.judge_json["action"] == "allow"
    assert state.ocr_text == ""


def test_ollama_provider_keeps_docling(monkeypatch):
    monkeypatch.setattr(settings, "vision_judge", True)
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    seen = {}
    monkeypatch.setattr(graph.url_agent, "run", lambda *a, **k: seen.update(k) or _fake_result())
    monkeypatch.setattr(graph.ocr_agent, "extract_text", lambda shots: "ocr words")
    state = graph.node_ocr(_state(["/9j/a"]))
    assert seen.get("extra_text") == "ocr words"
    assert "images_b64" not in seen
    assert state.ocr_text == "ocr words"


def test_vision_flag_off_keeps_docling(monkeypatch):
    monkeypatch.setattr(settings, "vision_judge", False)
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    assert not graph._vision_judge_active()


# --- OCR timeout ---------------------------------------------------------------------

def test_ocr_timeout_returns_empty(monkeypatch):
    from watchit_agents.agents import ocr_agent as module

    monkeypatch.setattr(module, "ocr_image_b64", lambda b64: time.sleep(5) or "late")
    monkeypatch.setattr(OCRAgent, "TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    assert OCRAgent().extract_text(["abc"]) == ""
    assert time.monotonic() - started < 2  # did not wait for the sleeping thread


def test_ocr_failure_returns_empty(monkeypatch):
    from watchit_agents.agents import ocr_agent as module

    monkeypatch.setattr(module, "ocr_image_b64", lambda b64: (_ for _ in ()).throw(RuntimeError("bad frame")))
    assert OCRAgent().extract_text(["abc"]) == ""


# --- upload limits + dedup -------------------------------------------------------------

def _device_ctx():
    return {"device": {"id": "dev_1", "household_id": "hh_1", "child_id": "c_1"}}


def _upgrade(data_json):
    return main.UpgradeInput(id="evt_1", child_id="c_1", ts=1, kind="visit", url="https://x.com", data_json=data_json)


def test_upgrade_rejects_oversized_payload(monkeypatch):
    monkeypatch.setattr(main.db, "get_event_household", lambda eid: "hh_1")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.post_event_upgrade(_upgrade("x" * (main._UPGRADE_MAX_BYTES + 1)), _device_ctx()))
    assert exc.value.status_code == 413


def test_upgrade_rejects_too_many_screenshots(monkeypatch):
    monkeypatch.setattr(main.db, "get_event_household", lambda eid: "hh_1")
    payload = json.dumps({"screenshots_b64": ["a", "b", "c", "d"]})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.post_event_upgrade(_upgrade(payload), _device_ctx()))
    assert exc.value.status_code == 400


def test_upgrade_dedups_pending_job(monkeypatch):
    monkeypatch.setattr(main.db, "get_event_household", lambda eid: "hh_1")
    monkeypatch.setattr(main.db, "find_pending_upgrade_job", lambda eid: {"id": "job_existing", "status": "pending"}, raising=False)
    monkeypatch.setattr(main.db, "enqueue_event_job", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not enqueue")))
    out = asyncio.run(main.post_event_upgrade(_upgrade(json.dumps({"screenshots_b64": ["a"]})), _device_ctx()))
    assert out["job_id"] == "job_existing" and out["status"] == "queued"
