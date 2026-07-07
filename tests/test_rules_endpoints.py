"""Guardian rules CRUD: validation + household scoping."""
import asyncio

import pytest
from fastapi import HTTPException

from watchit_api import main


def _ctx():
    return {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}


def _silence_audit(monkeypatch):
    monkeypatch.setattr(main.db, "log_audit", lambda *a, **k: None)


def test_create_rule_normalizes_pasted_url_to_domain(monkeypatch):
    _silence_audit(monkeypatch)
    captured = {}

    def fake_create(household_id, **kwargs):
        captured.update(kwargs, household_id=household_id)
        return {"id": "rule_1", **kwargs}

    monkeypatch.setattr(main.db, "create_rule", fake_create)
    payload = main.RuleCreatePayload(action="block", rule_type="domain", pattern="https://www.Roblox.com/games?x=1")
    asyncio.run(main.create_rule(payload, guardian_ctx=_ctx()))
    assert captured["pattern"] == "roblox.com"
    assert captured["household_id"] == "hh_1"


def test_create_rule_rejects_garbage_domain(monkeypatch):
    _silence_audit(monkeypatch)
    payload = main.RuleCreatePayload(action="block", rule_type="domain", pattern="not a domain")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.create_rule(payload, guardian_ctx=_ctx()))
    assert exc.value.status_code == 400


def test_create_rule_rejects_foreign_child(monkeypatch):
    _silence_audit(monkeypatch)
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid=None: None)
    payload = main.RuleCreatePayload(action="allow", rule_type="domain", pattern="example.com", child_id="child_elsewhere")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.create_rule(payload, guardian_ctx=_ctx()))
    assert exc.value.status_code == 404


def test_delete_rule_404_outside_household(monkeypatch):
    _silence_audit(monkeypatch)
    monkeypatch.setattr(main.db, "delete_rule", lambda rid, hid: 0)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.delete_rule("rule_other", guardian_ctx=_ctx()))
    assert exc.value.status_code == 404


def test_device_scoped_rule_requires_child(monkeypatch):
    _silence_audit(monkeypatch)
    payload = main.RuleCreatePayload(action="block", rule_type="domain", pattern="example.com", device_id="dev_1")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.create_rule(payload, guardian_ctx=_ctx()))
    assert exc.value.status_code == 400
