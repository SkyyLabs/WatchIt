"""Pairing redeem: shared-store throttling + failure handling."""
import asyncio
import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from watchit_api import main
from watchit_core.db import Database


def _request(ip="1.2.3.4"):
    return SimpleNamespace(client=SimpleNamespace(host=ip))


def _payload():
    return main.PairingRedeemPayload(code="123456", install_id="inst-1")


def test_redeem_checks_ip_and_global_scopes(monkeypatch):
    seen = {}

    def fake_allow(scopes, window_seconds):
        seen["scopes"] = scopes
        seen["window"] = window_seconds
        return True

    monkeypatch.setattr(main.db, "rate_limit_allow", fake_allow, raising=False)
    assert main._redeem_allowed("1.2.3.4")
    assert seen["scopes"] == [
        ("redeem_ip:1.2.3.4", main._REDEEM_MAX_PER_IP),
        ("redeem_global", main._REDEEM_MAX_GLOBAL),
    ]
    assert seen["window"] == main._REDEEM_WINDOW_SECONDS


def test_rate_limit_allow_is_shared_store():
    src = inspect.getsource(Database.rate_limit_allow)
    # prune expired, count within window, record only when allowed — one txn
    assert "DELETE FROM rate_limit_hits" in src
    assert "GROUP BY scope" in src
    assert "INSERT INTO rate_limit_hits" in src
    # deny path must not record a hit
    assert src.index("return False") < src.index("INSERT INTO rate_limit_hits")


def test_limits_unchanged():
    assert main._REDEEM_WINDOW_SECONDS == 60
    assert main._REDEEM_MAX_PER_IP == 10
    assert main._REDEEM_MAX_GLOBAL == 100


def test_route_returns_429_when_throttled(monkeypatch):
    monkeypatch.setattr(main, "_redeem_allowed", lambda ip: False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.redeem_pairing_code(_payload(), _request()))
    assert exc.value.status_code == 429


def test_route_returns_400_on_bad_code(monkeypatch):
    monkeypatch.setattr(main, "_redeem_allowed", lambda ip: True)
    monkeypatch.setattr(main.db, "redeem_pairing_code", lambda *a, **k: None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.redeem_pairing_code(_payload(), _request()))
    assert exc.value.status_code == 400


def test_retention_sweep_prunes_rate_limit_hits():
    assert "rate_limit_hits" in inspect.getsource(Database.run_retention_sweep)
