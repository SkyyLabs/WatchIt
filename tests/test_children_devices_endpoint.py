import pytest
from fastapi import HTTPException
from watchit_api import main


def _guardian_ctx():
    return {"household": {"id": "hh_1"}, "guardian": {"id": "g_1"}}


def test_devices_404_when_child_not_in_household(monkeypatch):
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid: None)
    with pytest.raises(HTTPException) as exc:
        main.list_child_devices("child_x", guardian_ctx=_guardian_ctx())
    assert exc.value.status_code == 404


def test_devices_returns_scoped_list(monkeypatch):
    monkeypatch.setattr(main.db, "get_child_profile", lambda cid, hid: {"id": cid})
    captured = {}
    def fake_fetch(household_id, child_id):
        captured["args"] = (household_id, child_id)
        return [{"id": "dev_1", "device_name": "Chrome"}]
    monkeypatch.setattr(main.db, "fetch_devices", fake_fetch)
    out = main.list_child_devices("child_1", guardian_ctx=_guardian_ctx())
    assert out == {"devices": [{"id": "dev_1", "device_name": "Chrome"}]}
    assert captured["args"] == ("hh_1", "child_1")
