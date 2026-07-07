"""Pairing redeem: brute-force throttling + failure handling."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from watchit_api import main


def _request(ip="1.2.3.4"):
    return SimpleNamespace(client=SimpleNamespace(host=ip))


def _payload():
    return main.PairingRedeemPayload(code="123456", install_id="inst-1")


@pytest.fixture(autouse=True)
def reset_limiter():
    main._redeem_by_ip.clear()
    main._redeem_global.clear()
    yield
    main._redeem_by_ip.clear()
    main._redeem_global.clear()


def test_per_ip_limit_trips():
    now = 1000.0
    for _ in range(main._REDEEM_MAX_PER_IP):
        assert main._redeem_allowed("ip-a", now)
    assert not main._redeem_allowed("ip-a", now)
    # A different IP is unaffected by the per-IP limit.
    assert main._redeem_allowed("ip-b", now)


def test_window_slides():
    now = 1000.0
    for _ in range(main._REDEEM_MAX_PER_IP):
        assert main._redeem_allowed("ip-a", now)
    assert main._redeem_allowed("ip-a", now + main._REDEEM_WINDOW_SECONDS + 1)


def test_global_limit_trips():
    now = 1000.0
    for i in range(main._REDEEM_MAX_GLOBAL):
        assert main._redeem_allowed(f"ip-{i}", now)
    assert not main._redeem_allowed("ip-fresh", now)


def test_route_returns_429_when_throttled(monkeypatch):
    monkeypatch.setattr(main, "_redeem_allowed", lambda ip: False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.redeem_pairing_code(_payload(), _request()))
    assert exc.value.status_code == 429


def test_route_returns_400_on_bad_code(monkeypatch):
    monkeypatch.setattr(main.db, "redeem_pairing_code", lambda *a, **k: None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.redeem_pairing_code(_payload(), _request()))
    assert exc.value.status_code == 400
