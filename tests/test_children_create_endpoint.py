import asyncio
import pytest
from fastapi import HTTPException
from watchit_api import main


def _ctx():
    return {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}


def test_create_rejects_out_of_range_age(monkeypatch):
    body = main.ChildCreatePayload(child_id="child_1", name="Kid", age=2)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.create_child(body, guardian_ctx=_ctx()))
    assert exc.value.status_code == 400


def test_create_requires_name(monkeypatch):
    body = main.ChildCreatePayload(child_id="child_1", name="   ", age=10)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.create_child(body, guardian_ctx=_ctx()))
    assert exc.value.status_code == 400


def test_create_adds_and_activates(monkeypatch):
    calls = {}
    monkeypatch.setattr(main.db, "add_child_profile", lambda cid, household_id=None, name=None: calls.setdefault("add", (cid, household_id, name)))
    monkeypatch.setattr(main.db, "update_child_profile", lambda cid, **kw: calls.setdefault("update", (cid, kw)))
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid: {"id": cid, "name": "Kid"})
    monkeypatch.setattr(main.db, "set_active_child_id", lambda cid, hid, gid: calls.setdefault("active", (cid, hid, gid)))
    monkeypatch.setattr(main.db, "log_audit", lambda *a, **k: None)
    body = main.ChildCreatePayload(child_id="child_1", name="Kid", strictness="strict", age=10)
    out = asyncio.run(main.create_child(body, guardian_ctx=_ctx()))
    assert out == {"child": {"id": "child_1", "name": "Kid"}}
    assert calls["add"][0] == "child_1" and calls["add"][1] == "hh_1"
    assert calls["active"] == ("child_1", "hh_1", "g_1")
