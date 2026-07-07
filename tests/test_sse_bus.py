"""Shared SSE bus: pg_notify publish path, payload cap, listener bridge."""
import asyncio
import json

from watchit_agents import runtime
from watchit_agents.runtime import DecisionBus, NOTIFY_PAYLOAD_LIMIT, _notify_payload


def _publish(bus, message):
    asyncio.run(bus.publish(message))


# --- publish routing -----------------------------------------------------------

def test_memory_mode_delivers_locally(monkeypatch):
    monkeypatch.setattr(runtime.settings, "sse_bus", "memory")
    called = []
    monkeypatch.setattr(runtime.db, "notify_decision", lambda payload: called.append(payload), raising=False)
    bus = DecisionBus()
    q = bus.subscribe()
    _publish(bus, {"decision_id": "d1", "household_id": "hh_1"})
    assert q.get_nowait()["decision_id"] == "d1"
    assert called == []


def test_postgres_mode_notifies_not_local(monkeypatch):
    monkeypatch.setattr(runtime.settings, "sse_bus", "postgres")
    sent = []
    monkeypatch.setattr(runtime.db, "notify_decision", lambda payload: sent.append(payload), raising=False)
    bus = DecisionBus()
    q = bus.subscribe()
    _publish(bus, {"decision_id": "d2", "household_id": "hh_1"})
    assert json.loads(sent[0])["decision_id"] == "d2"
    assert q.empty()  # local subscribers are fed by the listener, not publish


def test_postgres_notify_failure_falls_back_local(monkeypatch):
    monkeypatch.setattr(runtime.settings, "sse_bus", "postgres")

    def boom(payload):
        raise RuntimeError("db down")

    monkeypatch.setattr(runtime.db, "notify_decision", boom, raising=False)
    bus = DecisionBus()
    q = bus.subscribe()
    _publish(bus, {"decision_id": "d3"})
    assert q.get_nowait()["decision_id"] == "d3"


# --- payload cap ----------------------------------------------------------------

def test_notify_payload_under_limit_untouched():
    message = {"decision_id": "d1", "llm_rationale": "short", "title": "t", "url": "https://a.com"}
    assert json.loads(_notify_payload(message)) == message


def test_notify_payload_sheds_free_text_first():
    message = {"decision_id": "d1", "llm_rationale": "x" * 9000, "title": "t", "url": "https://a.com"}
    out = json.loads(_notify_payload(message))
    assert out["llm_rationale"] is None and out["title"] is None
    assert out["url"] == "https://a.com"  # url kept when shedding text suffices


def test_notify_payload_truncates_pathological_url():
    message = {"decision_id": "d1", "llm_rationale": None, "title": None, "url": "https://a.com/" + "p" * 9000}
    payload = _notify_payload(message)
    assert len(payload.encode()) <= NOTIFY_PAYLOAD_LIMIT
    assert len(json.loads(payload)["url"]) == 2000


# --- listener bridge --------------------------------------------------------------

def test_listener_bridges_payload_into_local_bus():
    from watchit_api import main

    q = main.bus.subscribe()
    try:
        asyncio.run(main._deliver_notify_payload(json.dumps({"decision_id": "d9", "household_id": "hh_1"})))
        assert q.get_nowait()["decision_id"] == "d9"
        asyncio.run(main._deliver_notify_payload("not-json"))
        assert q.empty()
    finally:
        main.bus.unsubscribe(q)
