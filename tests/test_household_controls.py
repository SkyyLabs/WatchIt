import asyncio
import pytest
from fastapi import HTTPException
from watchit_api import auth, main


def _ctx():
    return {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}


def _silence_audit(monkeypatch):
    monkeypatch.setattr(main.db, "log_audit", lambda *a, **k: None)


# --- active-household selection (auth scoping) -----------------------------

def test_select_household_rejects_unowned(monkeypatch):
    ctx = {"guardian": {"id": "g_1"}, "household": {"id": "hh_1"}}
    monkeypatch.setattr(auth.db, "list_guardian_households", lambda gid: [{"id": "hh_1", "name": "A"}])
    # An id the guardian doesn't belong to falls back to the default household.
    assert auth._select_active_household(ctx, "hh_evil")["household"]["id"] == "hh_1"


def test_select_household_switches_to_owned(monkeypatch):
    ctx = {"guardian": {"id": "g_1"}, "household": {"id": "hh_1"}}
    monkeypatch.setattr(auth.db, "list_guardian_households", lambda gid: [{"id": "hh_1"}, {"id": "hh_2", "name": "B"}])
    assert auth._select_active_household(ctx, "hh_2")["household"]["id"] == "hh_2"


# --- move child between households ------------------------------------------

def test_move_child_rejects_non_member_target(monkeypatch):
    _silence_audit(monkeypatch)
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid=None: {"id": cid, "household_id": "hh_1"})
    monkeypatch.setattr(main.db, "guardian_in_household", lambda gid, hid: hid == "hh_1")  # not a member of target
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.move_child("child_a", main.ChildMovePayload(target_household_id="hh_other"), guardian_ctx=_ctx()))
    assert exc.value.status_code == 403


def test_move_child_moves_when_member_of_both(monkeypatch):
    _silence_audit(monkeypatch)
    moved = {}
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid=None: {"id": cid, "household_id": "hh_1"})
    monkeypatch.setattr(main.db, "guardian_in_household", lambda gid, hid: True)
    monkeypatch.setattr(main.db, "move_child", lambda cid, src, dst: moved.setdefault("mv", (cid, src, dst)) or True)
    out = asyncio.run(main.move_child("child_a", main.ChildMovePayload(target_household_id="hh_2"), guardian_ctx=_ctx()))
    assert out == {"ok": True}
    assert moved["mv"] == ("child_a", "hh_1", "hh_2")


# --- household monitoring switch -------------------------------------------

def test_set_household_monitoring_persists(monkeypatch):
    _silence_audit(monkeypatch)
    saved = {}
    monkeypatch.setattr(main.db, "set_household_monitoring_enabled", lambda enabled, hid, gid: saved.setdefault("v", (enabled, hid)))
    out = asyncio.run(main.set_household_monitoring(main.MonitoringTogglePayload(enabled=False), guardian_ctx=_ctx()))
    assert out == {"ok": True, "monitoring_enabled": False}
    assert saved["v"] == (False, "hh_1")
