import asyncio
import time
import pytest
from fastapi import HTTPException
from watchit_api import main


def _ctx():
    return {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}


def test_resume_deletes_setting(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.db, "delete_setting", lambda k, hid: seen.setdefault("k", (k, hid)))
    out = asyncio.run(main.patch_control(main.ControlPatchPayload(paused_until_minutes=0), guardian_ctx=_ctx()))
    assert out == {"ok": True, "paused_until": None}
    assert seen["k"] == ("paused_until", "hh_1")


def test_pause_requires_pin(monkeypatch):
    monkeypatch.setattr(main.db, "is_parent_pin_set", lambda hid: True)
    monkeypatch.setattr(main.db, "verify_parent_pin", lambda pin, hid: False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.patch_control(main.ControlPatchPayload(paused_until_minutes=30, pin="0000"), guardian_ctx=_ctx()))
    assert exc.value.status_code == 403


def test_pause_sets_setting(monkeypatch):
    monkeypatch.setattr(main.db, "is_parent_pin_set", lambda hid: True)
    monkeypatch.setattr(main.db, "verify_parent_pin", lambda pin, hid: True)
    saved = {}
    monkeypatch.setattr(main.db, "set_setting", lambda k, v, hid, gid: saved.setdefault("v", (k, v)))
    out = asyncio.run(main.patch_control(main.ControlPatchPayload(paused_until_minutes=30, pin="1234"), guardian_ctx=_ctx()))
    assert out["paused_until"] > int(time.time() * 1000)
    assert saved["v"][0] == "paused_until"
