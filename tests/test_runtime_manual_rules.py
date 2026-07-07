"""Worker pipeline: a guardian manual rule decides before cache/LLM and wins
over any stale cached decision (rules gate sits before the URL cache)."""
import asyncio

from watchit_agents import runtime


def _event():
    return {
        "id": "evt_1", "household_id": "hh_1", "child_id": "child_1",
        "device_id": "dev_1", "url": "https://roblox.com/games", "title": "Games",
        "tab_id": "c-1", "ts": 1, "kind": "visit", "monitoring_enabled": True,
    }


def test_manual_block_rule_short_circuits_pipeline(monkeypatch):
    rule = {"id": "rule_9", "household_id": "hh_1", "child_id": "child_1", "device_id": None,
            "action": "block", "rule_type": "domain", "pattern": "roblox.com",
            "expires_at": None, "enabled": True}
    monkeypatch.setattr(runtime.db, "get_active_child_id", lambda h: None)
    monkeypatch.setattr(runtime.db, "add_event", lambda e: e["id"])
    monkeypatch.setattr(runtime.db, "get_paused_until", lambda h: None)
    monkeypatch.setattr(runtime.db, "get_device_paused_until", lambda d: None)
    monkeypatch.setattr(runtime.db, "get_effective_quiet_schedules", lambda h, c, d=None: [])
    monkeypatch.setattr(runtime.db, "list_active_rules", lambda h, c, d: [rule])
    recorded = {}

    def fake_add_decision(event_id, policy_version, action, reason, details):
        recorded.update(action=action, reason=reason, details=details)
        return "dec_1"

    monkeypatch.setattr(runtime.db, "add_decision", fake_add_decision)
    # The cache/graph must never be reached — poison them.
    monkeypatch.setattr(runtime.db, "get_cached_url_decision", lambda *a, **k: (_ for _ in ()).throw(AssertionError("cache reached")))

    message = asyncio.run(runtime.process_event(_event()))
    assert message["action"] == "block"
    assert message["reason"] == "manual_rule:rule_9"
    assert recorded["action"] == "block"
    assert recorded["details"]["rule_id"] == "rule_9"


def test_no_rule_match_falls_through_to_cache(monkeypatch):
    monkeypatch.setattr(runtime.db, "get_active_child_id", lambda h: None)
    monkeypatch.setattr(runtime.db, "add_event", lambda e: e["id"])
    monkeypatch.setattr(runtime.db, "get_paused_until", lambda h: None)
    monkeypatch.setattr(runtime.db, "get_device_paused_until", lambda d: None)
    monkeypatch.setattr(runtime.db, "get_effective_quiet_schedules", lambda h, c, d=None: [])
    monkeypatch.setattr(runtime.db, "list_active_rules", lambda h, c, d: [])
    monkeypatch.setattr(runtime.db, "get_child_profile", lambda c, h=None: {"id": c, "strictness": "standard", "age": 12})
    monkeypatch.setattr(runtime.db, "add_analysis", lambda *a, **k: "ana_1")
    monkeypatch.setattr(runtime.db, "add_decision", lambda *a, **k: "dec_1")
    cached = {
        "action": "allow", "reason": "url_cache", "details_json": {"confidence": 0.95},
        "source": "pipeline", "source_decision_id": "dec_0",
    }
    monkeypatch.setattr(runtime.db, "get_cached_url_decision", lambda *a, **k: cached)
    message = asyncio.run(runtime.process_event(_event()))
    assert message["action"] == "allow"
