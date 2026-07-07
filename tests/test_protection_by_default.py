"""Protection by default: pairing implies monitoring; ingest self-heals a
missing session unless a guardian explicitly stopped the child."""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from watchit_api import main
from watchit_core.db import Database


def _device_ctx():
    return {"device": {"id": "dev_1", "child_id": "c1", "household_id": "hh_1"}}


def _evt():
    return main.EventInput(child_id="paired", ts=1, kind="visit", url="https://example.com")


def test_ingest_uses_ensure_not_get(monkeypatch):
    # The route must call the self-healing ensure_monitoring_session; 409 only
    # when it returns None (explicit guardian stop).
    monkeypatch.setattr(main.db, "ensure_monitoring_session", lambda h, c, d: {"id": "sess_1"})
    monkeypatch.setattr(main.db, "is_household_monitoring_enabled", lambda h: True)
    monkeypatch.setattr(main.db, "enqueue_event_job", lambda event, upgrade: ("job_1", "evt_1"))
    monkeypatch.setattr(main.settings, "processing_mode", "async")
    out = asyncio.run(main.post_event(_evt(), device_ctx=_device_ctx()))
    assert out["status"] == "queued"


def test_ingest_409_only_after_guardian_stop(monkeypatch):
    monkeypatch.setattr(main.db, "ensure_monitoring_session", lambda h, c, d: None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.post_event(_evt(), device_ctx=_device_ctx()))
    assert exc.value.status_code == 409


def test_ensure_respects_guardian_stop_and_audits():
    src = inspect.getsource(Database.ensure_monitoring_session)
    assert "guardian_stopped" in src
    assert "monitoring_autostarted" in src
    # scoped inserts
    assert "household_id" in src and "child_id" in src


def test_redeem_starts_device_scoped_session_without_stopping_others():
    src = inspect.getsource(Database.redeem_pairing_code)
    assert "INSERT INTO monitoring_sessions" in src
    # must check for an existing applicable session, never stop other devices' sessions
    assert "status='active'" in src
    assert "stop_reason='replaced'" not in src


def test_redeem_route_audits_autostart(monkeypatch):
    audits = []
    monkeypatch.setattr(main, "_redeem_allowed", lambda ip: True)
    monkeypatch.setattr(main.db, "log_audit", lambda hid, action, **k: audits.append(action))
    monkeypatch.setattr(
        main.db, "redeem_pairing_code",
        lambda *a, **k: {
            "device": {"id": "dev_1", "household_id": "hh_1", "child_id": "c1"},
            "device_token": "wdev_x", "reassigned": None,
            "session": {"id": "sess_1"},
        },
    )
    from types import SimpleNamespace
    request = SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"))
    payload = main.PairingRedeemPayload(code="123456", install_id="inst-1")
    out = asyncio.run(main.redeem_pairing_code(payload, request))
    assert out["device"]["id"] == "dev_1"
    assert "device_paired" in audits
    assert "monitoring_autostarted" in audits
