"""Device token lifecycle: structured auth errors, expiry, rotation grace."""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from watchit_api import auth
from watchit_core.db import Database


def _reject(monkeypatch, error):
    monkeypatch.setattr(auth.db, "resolve_device_token", lambda _t: (None, error))


def test_revoked_device_gets_structured_401(monkeypatch):
    _reject(monkeypatch, "device_revoked")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_device(authorization="Bearer wdev_x"))
    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "device_revoked"


def test_expired_token_gets_structured_401(monkeypatch):
    _reject(monkeypatch, "token_expired")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_device(authorization="Bearer wdev_x"))
    assert exc.value.detail["code"] == "token_expired"


def test_invalid_token_gets_structured_401(monkeypatch):
    _reject(monkeypatch, "invalid")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_device_stream(token="wdev_x"))
    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "invalid"


def test_valid_token_resolves_device(monkeypatch):
    device = {"id": "dev_1", "child_id": "c1", "household_id": "hh_1", "status": "active"}
    monkeypatch.setattr(auth.db, "resolve_device_token", lambda _t: (device, None))
    ctx = asyncio.run(auth.require_device(authorization="Bearer wdev_x"))
    assert ctx["device"]["id"] == "dev_1"


# The expiry/grace/rotation logic is SQL; assert the load-bearing clauses exist
# (consistent with the repo's repository-source tests).

def test_resolve_checks_expiry_and_rotation_grace():
    src = inspect.getsource(Database.resolve_device_token)
    assert "token_expires_at" in src
    assert "token_prev_hash" in src
    assert "token_rotated_at" in src


def test_rotate_keeps_previous_hash_and_sets_expiry():
    src = inspect.getsource(Database.rotate_device_token)
    assert "token_prev_hash=token_hash" in src
    assert "token_expires_at=now()" in src
    assert "status='active'" in src


def test_redeem_issues_expiring_token_and_clears_grace():
    src = inspect.getsource(Database.redeem_pairing_code)
    assert "token_expires_at" in src
    assert "token_prev_hash=NULL" in src
