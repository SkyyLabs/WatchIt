from watchit_api import main


def _device_ctx():
    return {"device": {"id": "dev_1", "household_id": "hh_1", "child_id": "child_1"}}


def test_event_decision_pending_when_no_decision(monkeypatch):
    monkeypatch.setattr(main.db, "get_decision_by_event", lambda eid, hid: None)
    out = main.get_event_decision("evt_1", device_ctx=_device_ctx())
    assert out == {"status": "pending"}


def test_event_decision_scopes_by_device_household(monkeypatch):
    captured = {}

    def fake_lookup(event_id, household_id):
        captured["args"] = (event_id, household_id)
        return {
            "id": "dec_1",
            "event_id": event_id,
            "household_id": household_id,
            "action": "block",
            "reason": "policy",
            "details_json": {"categories": ["adult"], "rationale": "unsafe"},
            "url": "https://example.com",
            "tab_id": "c-9",
        }

    monkeypatch.setattr(main.db, "get_decision_by_event", fake_lookup)
    out = main.get_event_decision("evt_1", device_ctx=_device_ctx())
    assert captured["args"] == ("evt_1", "hh_1")
    assert out["status"] == "decided"
    assert out["decision"]["action"] == "block"
    assert out["decision"]["categories"] == ["adult"]
    assert out["decision"]["url"] == "https://example.com"
