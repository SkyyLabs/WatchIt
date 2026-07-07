"""/v1/event/upgrade must not write into another household's events (gap A1)."""
import asyncio

import pytest
from fastapi import HTTPException

from watchit_api import main


def _device_ctx(household="hh_1"):
    return {"device": {"id": "dev_1", "child_id": "c1", "household_id": household}}


def _evt():
    return main.UpgradeInput(id="evt_target", child_id="paired", ts=1, kind="content")


def test_cross_household_upgrade_rejected(monkeypatch):
    monkeypatch.setattr(main.db, "get_event_household", lambda _eid: "hh_other")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.post_event_upgrade(_evt(), device_ctx=_device_ctx("hh_1")))
    assert exc.value.status_code == 403


def test_unknown_event_upgrade_rejected(monkeypatch):
    monkeypatch.setattr(main.db, "get_event_household", lambda _eid: None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.post_event_upgrade(_evt(), device_ctx=_device_ctx()))
    assert exc.value.status_code == 404


def test_same_household_upgrade_enqueues(monkeypatch):
    monkeypatch.setattr(main.db, "get_event_household", lambda _eid: "hh_1")
    monkeypatch.setattr(main.db, "get_active_monitoring_session", lambda *a, **k: {"id": "sess_1"})
    monkeypatch.setattr(main.db, "is_household_monitoring_enabled", lambda _h: True)
    monkeypatch.setattr(main.db, "enqueue_event_job", lambda event, upgrade: ("job_1", event["id"]))
    monkeypatch.setattr(main.settings, "processing_mode", "async")
    out = asyncio.run(main.post_event_upgrade(_evt(), device_ctx=_device_ctx("hh_1")))
    assert out["status"] == "queued"
    assert out["event_id"] == "evt_target"
