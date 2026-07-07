"""Review queue + rule tester: scoping and layer ordering."""
import inspect

import pytest
from fastapi import HTTPException

from watchit_api import main
from watchit_core.db import Database


def _ctx():
    return {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}


# --- review queue ------------------------------------------------------------

def test_review_query_scopes_and_excludes_overridden():
    src = inspect.getsource(Database.list_review_decisions)
    assert "household_id=%s" in src
    assert "o.id IS NULL" in src           # already-reviewed items drop out
    assert "pending_ocr" in src and "system_uncertain" in src
    assert "confidence" in src             # low-confidence judgments included


def test_review_endpoint_scopes_household_and_child(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.db, "list_review_decisions", lambda hid, cid: seen.update(hid=hid, cid=cid) or [])
    out = main.get_review_queue(child_id="child_1", guardian_ctx=_ctx())
    assert seen == {"hid": "hh_1", "cid": "child_1"}
    assert out == {"items": []}


# --- rule tester (dry-run layering) -------------------------------------------

def _stub_layers(monkeypatch, *, rules=None, schedules=None, cached=None):
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid=None: {"id": cid, "strictness": "standard", "age": 12})
    monkeypatch.setattr(main.db, "list_active_rules", lambda h, c, d: rules or [])
    monkeypatch.setattr(main.db, "get_effective_quiet_schedules", lambda h, c, d: schedules or [])
    monkeypatch.setattr(main.db, "peek_url_decision", lambda key, ttl, hid: cached)


def _test(url):
    return main.test_rules(main.RuleTestPayload(url=url, child_id="c1"), guardian_ctx=_ctx())


def test_tester_404_for_foreign_child(monkeypatch):
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid=None: None)
    with pytest.raises(HTTPException) as exc:
        _test("https://example.com")
    assert exc.value.status_code == 404


def test_tester_rule_layer_wins(monkeypatch):
    rule = {"id": "r1", "action": "block", "rule_type": "domain", "pattern": "roblox.com",
            "child_id": "c1", "device_id": None, "expires_at": None, "enabled": True}
    _stub_layers(monkeypatch, rules=[rule])
    out = _test("https://roblox.com/games")
    assert out["layer"] == "rule" and out["action"] == "block" and out["rule_id"] == "r1"


def test_tester_schedule_layer(monkeypatch):
    all_day = {"days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun", "quiet_start": "00:00", "quiet_end": "23:59", "timezone": None}
    _stub_layers(monkeypatch, schedules=[all_day])
    out = _test("https://example.com")
    assert out["layer"] == "schedule" and out["action"] == "block"


def test_tester_cache_layer(monkeypatch):
    _stub_layers(monkeypatch, cached={"action": "blur", "reason": "llm:medium"})
    out = _test("https://example.com/x")
    assert out["layer"] == "cache" and out["action"] == "blur"


def test_tester_policy_layer(monkeypatch):
    _stub_layers(monkeypatch)
    assert _test("https://en.wikipedia.org/wiki/Math")["layer"] == "policy"
    assert _test("https://pornhub.com/")["action"] == "block"


def test_tester_falls_through_to_ai(monkeypatch):
    _stub_layers(monkeypatch)
    out = _test("example.com/somewhere")  # scheme-less input accepted
    assert out["layer"] == "ai" and out["action"] == "unknown"
