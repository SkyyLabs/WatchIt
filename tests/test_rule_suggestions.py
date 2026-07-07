"""Learned rules: suggestion generation guards, guardian review endpoints."""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from watchit_api import main
from watchit_core.db import Database


def _ctx():
    return {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}


def _suggestion(**over):
    row = {
        "id": "rsug_1", "household_id": "hh_1", "child_id": "c_1", "action": "allow",
        "rule_type": "domain", "pattern": "scratch.mit.edu", "evidence_count": 4,
        "status": "accepted",
    }
    row.update(over)
    return row


# --- generation query guards -------------------------------------------------------

def test_generation_thresholds_and_guards():
    src = inspect.getsource(Database.generate_rule_suggestions)
    assert Database.SUGGESTION_MIN_OVERRIDES == 3
    assert Database.SUGGESTION_WINDOW_DAYS == 30
    assert "HAVING COUNT(*) >=" in src
    assert "o.action IN ('allow', 'block')" in src
    # never re-suggest over an existing rule, a pending suggestion, or a recent dismissal
    assert "policy_rules" in src and "r.enabled = TRUE" in src
    assert "s.status = 'pending'" in src
    assert "s.status = 'dismissed'" in src


def test_resolution_only_touches_pending_household_rows():
    src = inspect.getsource(Database.resolve_rule_suggestion)
    assert "household_id=%s" in src and "status='pending'" in src


def test_learning_loop_generates_before_early_return():
    from watchit_learning.guardian_learning import GuardianLearningLoop

    src = inspect.getsource(GuardianLearningLoop.process_once)
    assert src.index("generate_rule_suggestions") < src.index("fetch_unprocessed_overrides")


# --- endpoints ----------------------------------------------------------------------

def test_list_scopes_household(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.db, "list_rule_suggestions", lambda hid: seen.update(hid=hid) or [], raising=False)
    out = main.list_rule_suggestions(guardian_ctx=_ctx())
    assert seen == {"hid": "hh_1"}
    assert out == {"suggestions": []}


def test_accept_mints_rule_with_suggestion_scope(monkeypatch):
    minted = {}
    monkeypatch.setattr(
        main.db, "resolve_rule_suggestion",
        lambda sid, hid, status, guardian_id: _suggestion() if hid == "hh_1" else None,
        raising=False,
    )

    def fake_create(household_id, **kw):
        minted.update(household_id=household_id, **kw)
        return {"id": "rule_new", **kw}

    monkeypatch.setattr(main.db, "create_rule", fake_create)
    monkeypatch.setattr(main.db, "log_audit", lambda *a, **k: None)
    out = asyncio.run(main.accept_rule_suggestion("rsug_1", guardian_ctx=_ctx()))
    assert out["rule"]["id"] == "rule_new"
    assert minted["household_id"] == "hh_1"
    assert minted["child_id"] == "c_1"
    assert minted["pattern"] == "scratch.mit.edu"
    assert minted["action"] == "allow"
    assert minted["created_by_guardian_id"] == "g_1"


def test_accept_404_for_foreign_or_resolved(monkeypatch):
    monkeypatch.setattr(main.db, "resolve_rule_suggestion", lambda *a, **k: None, raising=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.accept_rule_suggestion("rsug_other", guardian_ctx=_ctx()))
    assert exc.value.status_code == 404


def test_dismiss_marks_without_minting(monkeypatch):
    monkeypatch.setattr(
        main.db, "resolve_rule_suggestion",
        lambda sid, hid, status, guardian_id: _suggestion(status="dismissed") if status == "dismissed" else None,
        raising=False,
    )
    monkeypatch.setattr(main.db, "create_rule", lambda *a, **k: (_ for _ in ()).throw(AssertionError("dismiss must not mint a rule")))
    monkeypatch.setattr(main.db, "log_audit", lambda *a, **k: None)
    out = asyncio.run(main.dismiss_rule_suggestion("rsug_1", guardian_ctx=_ctx()))
    assert out == {"ok": True}
