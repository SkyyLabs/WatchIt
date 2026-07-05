import asyncio
import time
import pytest
from fastapi import HTTPException
from watchit_api import main


def _ctx():
    return {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}


def _payload(minutes, pin=None):
    return main.DevicePatchPayload(paused_until_minutes=minutes, pin=pin)


def test_resume_clears_pause(monkeypatch):
    calls = {}
    monkeypatch.setattr(main.db, "clear_device_pause", lambda hid, did: calls.setdefault("cleared", (hid, did)) or 1)
    monkeypatch.setattr(main.db, "log_audit", lambda *args, **kwargs: None)
    out = asyncio.run(main.patch_device("dev_1", _payload(0), guardian_ctx=_ctx()))
    assert out == {"ok": True, "paused_until": None}
    assert calls["cleared"] == ("hh_1", "dev_1")


def test_pause_requires_pin(monkeypatch):
    monkeypatch.setattr(main.db, "is_parent_pin_set", lambda hid: True)
    monkeypatch.setattr(main.db, "verify_parent_pin", lambda pin, hid: False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.patch_device("dev_1", _payload(30, "0000"), guardian_ctx=_ctx()))
    assert exc.value.status_code == 403


def test_pause_sets_future_timestamp(monkeypatch):
    monkeypatch.setattr(main.db, "is_parent_pin_set", lambda hid: True)
    monkeypatch.setattr(main.db, "verify_parent_pin", lambda pin, hid: True)
    saved = {}
    monkeypatch.setattr(main.db, "set_device_pause", lambda hid, did, until: saved.setdefault("v", until) or 1)
    monkeypatch.setattr(main.db, "log_audit", lambda *args, **kwargs: None)
    out = asyncio.run(main.patch_device("dev_1", _payload(30, "1234"), guardian_ctx=_ctx()))
    assert out["ok"] is True
    assert out["paused_until"] > int(time.time() * 1000)
    assert saved["v"] == out["paused_until"]


def test_pause_unknown_device_404(monkeypatch):
    monkeypatch.setattr(main.db, "is_parent_pin_set", lambda hid: True)
    monkeypatch.setattr(main.db, "verify_parent_pin", lambda pin, hid: True)
    monkeypatch.setattr(main.db, "set_device_pause", lambda hid, did, until: 0)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.patch_device("nope", _payload(30, "1234"), guardian_ctx=_ctx()))
    assert exc.value.status_code == 404
