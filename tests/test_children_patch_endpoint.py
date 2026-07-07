import asyncio
import pytest
from fastapi import HTTPException
from watchit_api import main


def _ctx():
    return {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}


def test_patch_child_updates_scoped(monkeypatch):
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid: {"id": cid})
    seen = {}
    def fake_update(child_id, strictness=None, age=None, household_id=None, name=None, **kw):
        seen.update({"child_id": child_id, "household_id": household_id, "age": age})
    monkeypatch.setattr(main.db, "update_child_profile", fake_update)
    monkeypatch.setattr(main.db, "log_audit", lambda *a, **k: None)
    body = main.ChildSettingsPayload(name="Kid", age=10, strictness="strict")
    out = asyncio.run(main.patch_child("child_1", body, guardian_ctx=_ctx()))
    assert out["ok"] is True
    assert seen == {"child_id": "child_1", "household_id": "hh_1", "age": 10}


def test_patch_child_rejects_out_of_range_age(monkeypatch):
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid: {"id": cid})
    body = main.ChildSettingsPayload(name="Kid", age=99, strictness="strict")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.patch_child("child_1", body, guardian_ctx=_ctx()))
    assert exc.value.status_code == 400


def test_patch_child_404(monkeypatch):
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid: None)
    body = main.ChildSettingsPayload(name="Kid", age=10, strictness="strict")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.patch_child("nope", body, guardian_ctx=_ctx()))
    assert exc.value.status_code == 404
