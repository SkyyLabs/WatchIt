"""Queue hardening: attempt caps, stale-claim reaper, dead-letter visibility,
health endpoint."""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from watchit_api import main
from watchit_agents.worker import AgentWorker
from watchit_core.db import Database, db


def test_claim_caps_attempts():
    src = inspect.getsource(Database.claim_event_jobs)
    assert "attempts < %s" in src
    assert "FOR UPDATE SKIP LOCKED" in src  # multi-worker safety preserved


def test_reaper_requeues_then_dead_letters():
    src = inspect.getsource(Database.reap_stale_event_jobs)
    assert "status='pending'" in src and "attempts < %s" in src      # requeue path
    assert "status='failed'" in src and "attempts >= %s" in src      # dead-letter path


def test_worker_reap_is_time_gated(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "reap_stale_event_jobs", lambda: calls.append(1) or {"requeued": 0, "dead_lettered": 0})
    worker = AgentWorker(poll_interval=0)
    worker.maybe_reap_stale_jobs()
    worker.maybe_reap_stale_jobs()  # within the gate window — must not run again
    assert len(calls) == 1


def test_healthz_ok(monkeypatch):
    monkeypatch.setattr(main.db, "ping", lambda: True)
    assert main.healthz() == {"ok": True}


def test_healthz_503_when_db_down(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(main.db, "ping", boom)
    with pytest.raises(HTTPException) as exc:
        main.healthz()
    assert exc.value.status_code == 503


def test_queue_stats_scoped_to_household(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.db, "get_queue_stats", lambda hid: seen.setdefault("stats", hid) and {} or {"pending": 0})
    monkeypatch.setattr(main.db, "list_failed_event_jobs", lambda hid, limit=20: seen.setdefault("failed", hid) and [] or [])
    ctx = {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}
    out = main.get_queue_stats(guardian_ctx=ctx)
    assert seen == {"stats": "hh_1", "failed": "hh_1"}
    assert "queue" in out and "failed_jobs" in out


def test_household_devices_scoped(monkeypatch):
    seen = []
    monkeypatch.setattr(main.db, "fetch_household_devices", lambda hid: seen.append(hid) or [])
    ctx = {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}
    out = main.list_household_devices(guardian_ctx=ctx)
    assert seen == ["hh_1"]
    assert out == {"devices": []}
