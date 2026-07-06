import asyncio
import pytest
from fastapi import HTTPException
from watchit_api import main


def _ctx():
    return {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}


def _valid_schedule(**overrides):
    base = dict(days="Mon,Tue", quiet_start="21:00", quiet_end="07:00", enabled=True)
    base.update(overrides)
    return main.ScheduleUpsertPayload(**base)


def test_upsert_schedule_rejects_bad_time(monkeypatch):
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid=None: {"id": cid})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.upsert_child_schedule("child_a", _valid_schedule(quiet_start="9pm"), guardian_ctx=_ctx()))
    assert exc.value.status_code == 400


def test_upsert_schedule_rejects_bad_days(monkeypatch):
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid=None: {"id": cid})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.upsert_child_schedule("child_a", _valid_schedule(days="Funday"), guardian_ctx=_ctx()))
    assert exc.value.status_code == 400


def test_upsert_schedule_happy_path(monkeypatch):
    monkeypatch.setattr(main.db, "log_audit", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid=None: {"id": cid})
    monkeypatch.setattr(main.db, "upsert_schedule", lambda *a, **k: "sch_1")
    out = asyncio.run(main.upsert_child_schedule("child_a", _valid_schedule(), guardian_ctx=_ctx()))
    assert out == {"schedule_id": "sch_1"}
