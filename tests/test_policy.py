"""Policy engine decision guarantees (packages/core policy engine).

Cases are chosen to be time-of-day independent: hard blocklist domains block in
every branch, and educational allowlist domains allow even during quiet hours.
"""
from datetime import datetime

from watchit_core.policy.engine import PolicyEngine, _in_quiet_hours


def _event(url):
    return {"url": url}


def test_blocklist_domain_always_blocks():
    decision = PolicyEngine().decide(_event("https://pornhub.com/x"), {}, {})
    assert decision["action"] == "block"


def test_edu_allowlist_domain_always_allows():
    # wikipedia.org is in the allowlist and stays allowed even in quiet hours.
    decision = PolicyEngine().decide(_event("https://en.wikipedia.org/wiki/X"), {}, {})
    assert decision["action"] == "allow"


def test_prefilter_high_score_blocks():
    decision = PolicyEngine().decide(_event("https://news.example.com"), {"sexual": 0.99}, {})
    assert decision["action"] == "block"
    assert "sexual" in decision["categories"]


def test_strict_threshold_lower_than_standard():
    # 0.85 blocks under strict (0.8) but not standard (0.9).
    engine = PolicyEngine()
    strict = engine.decide(_event("https://news.example.com"), {"violence": 0.85}, {}, {"strictness": "strict"})
    standard = engine.decide(_event("https://news.example.com"), {"violence": 0.85}, {}, {"strictness": "standard"})
    assert strict["action"] == "block"
    assert standard["action"] != "block"


def test_quiet_hours_wraps_midnight():
    # 21:00-07:00 window: 23:00 is inside, 12:00 is outside (Monday is in default days).
    assert _in_quiet_hours(datetime(2026, 7, 6, 23, 0), "Mon,Tue,Wed,Thu", "21:00-07:00") is True
    assert _in_quiet_hours(datetime(2026, 7, 6, 12, 0), "Mon,Tue,Wed,Thu", "21:00-07:00") is False


def test_quiet_hours_ignores_out_of_schedule_days():
    # Sunday (2026-07-05) is not in the schedule days, so never quiet.
    assert _in_quiet_hours(datetime(2026, 7, 5, 23, 0), "Mon,Tue,Wed,Thu", "21:00-07:00") is False
