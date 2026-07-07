"""Policy snapshot: scoping, content, version stability."""
import asyncio

from watchit_core.db import Database
from watchit_api import main


def _stubbed_db(rules=None, schedules=None, cached=None, monitoring=True, session=True):
    db = Database()
    db.get_child_profile = lambda cid, hid=None: {"id": cid, "strictness": "strict", "age": 10}
    db.list_active_rules = lambda h, c, d: rules or []
    db.get_effective_quiet_schedules = lambda h, c, d: schedules or []
    db.is_household_monitoring_enabled = lambda h: monitoring
    db.get_active_monitoring_session = lambda h, c, d: ({"id": "sess"} if session else None)
    db.get_paused_until = lambda h: None
    db.list_snapshot_cached_decisions = lambda h, c: cached or []
    return db


def _device():
    return {
        "id": "dev_1", "household_id": "hh_1", "child_id": "child_1",
        "status": "active", "paused_until": None, "token_expires_at": None,
    }


def test_snapshot_carries_scope_and_state():
    snapshot = _stubbed_db().build_policy_snapshot(_device())
    assert snapshot["household_id"] == "hh_1"
    assert snapshot["child_id"] == "child_1"
    assert snapshot["device_id"] == "dev_1"
    assert snapshot["strictness"] == "strict"
    assert snapshot["monitoring_enabled"] is True
    assert snapshot["monitoring_active"] is True
    assert snapshot["expires_at"] > snapshot["issued_at"]
    assert snapshot["refresh_after_seconds"] > 0
    assert snapshot["high_risk_tokens"]


def test_snapshot_version_stable_for_identical_content():
    db = _stubbed_db(rules=[{"id": "r1", "action": "block", "rule_type": "domain",
                             "pattern": "roblox.com", "child_id": None, "device_id": None,
                             "expires_at": None, "reason": None}])
    v1 = db.build_policy_snapshot(_device())["version"]
    v2 = db.build_policy_snapshot(_device())["version"]
    assert v1 == v2


def test_snapshot_version_changes_when_rules_change():
    base = _stubbed_db()
    with_rule = _stubbed_db(rules=[{"id": "r1", "action": "block", "rule_type": "domain",
                                    "pattern": "roblox.com", "child_id": None, "device_id": None,
                                    "expires_at": None, "reason": None}])
    assert base.build_policy_snapshot(_device())["version"] != with_rule.build_policy_snapshot(_device())["version"]


def test_endpoint_stamps_policy_freshness(monkeypatch):
    stamped = []
    monkeypatch.setattr(main.db, "build_policy_snapshot", lambda device: {"version": "abc"})
    monkeypatch.setattr(main.db, "stamp_policy_fetch", lambda device_id: stamped.append(device_id))
    out = main.get_device_policy(device_ctx={"device": _device()})
    assert out["snapshot"]["version"] == "abc"
    assert stamped == ["dev_1"]
