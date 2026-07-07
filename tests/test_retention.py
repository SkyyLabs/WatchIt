"""Retention sweep + screenshot-bytes invariant + raw-LLM logging privacy."""
import inspect

from watchit_agents.worker import AgentWorker
from watchit_core.db import Database


# --- no screenshot bytes in Postgres ---------------------------------------------

def test_strip_screenshot_bytes_replaces_with_count():
    payload = {"dom_sample": "text", "screenshots_b64": ["aaa", "bbb"]}
    out = Database._strip_screenshot_bytes(payload)
    assert "screenshots_b64" not in out
    assert out["screenshot_count"] == 2
    assert out["dom_sample"] == "text"
    # original payload untouched (queue transport still carries the images)
    assert "screenshots_b64" in payload


def test_strip_screenshot_bytes_passthrough():
    assert Database._strip_screenshot_bytes({"dom_sample": "x"}) == {"dom_sample": "x"}
    assert Database._strip_screenshot_bytes([]) == []


def test_event_writes_go_through_strip():
    for method in (Database.add_event, Database.update_event_data_json):
        assert "_strip_screenshot_bytes" in inspect.getsource(method)


# --- retention sweep ---------------------------------------------------------------

def test_sweep_covers_every_retention_target():
    src = inspect.getsource(Database.run_retention_sweep)
    assert "data_json - 'dom_sample'" in src                      # 7d strip, row kept
    assert "status IN ('completed','failed')" in src              # finished jobs only
    assert "retention_expires_at" in src and "captured_at" in src  # screenshots
    assert "DELETE FROM events WHERE ts <" in src                 # 12mo cascade
    assert "DELETE FROM audit_log" in src
    assert "url_decision_cache" in src


def test_sweep_windows_match_privacy_doc():
    assert Database.RETENTION_DOM_SAMPLE_DAYS == 7
    assert Database.RETENTION_EVENT_JOBS_DAYS == 14
    assert Database.RETENTION_SCREENSHOTS_DAYS == 30
    assert Database.RETENTION_EVENTS_DAYS == 365
    assert Database.RETENTION_AUDIT_DAYS == 730


def test_screenshot_insert_stamps_retention():
    assert "retention_expires_at" in inspect.getsource(Database.insert_screenshot_file)


# --- worker wiring ------------------------------------------------------------------

def _worker(monkeypatch, enabled=True):
    from watchit_agents import worker as worker_module

    monkeypatch.setattr(worker_module.settings, "retention_sweep_enabled", enabled, raising=False)
    w = AgentWorker(poll_interval=0.01)
    return w, worker_module


def test_worker_sweep_flag_gated(monkeypatch):
    w, worker_module = _worker(monkeypatch, enabled=False)
    called = []
    monkeypatch.setattr(worker_module.db, "run_retention_sweep", lambda: called.append(1) or {}, raising=False)
    w.maybe_run_retention_sweep()
    assert called == []


def test_worker_sweep_hourly_gate(monkeypatch):
    w, worker_module = _worker(monkeypatch, enabled=True)
    calls = []
    monkeypatch.setattr(worker_module.db, "run_retention_sweep", lambda: calls.append(1) or {"events_purged": 0}, raising=False)
    w.maybe_run_retention_sweep()
    w.maybe_run_retention_sweep()  # inside the hour: skipped
    assert len(calls) == 1


# --- raw LLM output stays out of structlog -------------------------------------------

def test_llm_judge_logs_length_not_content():
    from watchit_agents import llm_judge

    src = inspect.getsource(llm_judge)
    assert "raw_len=len(raw)" in src
    assert 'logger.debug("llm_raw_response"' not in src
