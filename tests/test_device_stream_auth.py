"""Device-authenticated SSE stream: EventSource can't send headers, so the
decision stream must accept the device token via a query param and scope to
that device's household."""
import asyncio

import pytest
from fastapi import HTTPException

from watchit_api import auth


def test_missing_stream_token_rejected():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_device_stream(token=None))
    assert exc.value.status_code == 401


def test_invalid_stream_token_rejected(monkeypatch):
    monkeypatch.setattr(auth.db, "authenticate_device_token", lambda _t: None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_device_stream(token="bad"))
    assert exc.value.status_code == 401


def test_valid_device_token_resolves_household(monkeypatch):
    device = {"id": "dev_1", "child_id": "child_1", "household_id": "hh_1", "status": "active"}
    monkeypatch.setattr(auth.db, "authenticate_device_token", lambda _t: device)
    ctx = asyncio.run(auth.require_device_stream(token="good"))
    assert ctx["device"]["household_id"] == "hh_1"
