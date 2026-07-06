"""Deterministic graph routing + false-negative guards (services/agent-worker).

The graph must never allow a page through on a weak signal: uncertain headlines
escalate to the LLM, clearly-harmful LLM judgments block immediately, and the
OCR-pending interim is a warn (handled by runtime), never an allow. The agents
are stubbed so these assertions cover routing, not model behavior.
"""
import watchit_agents.graph as g
from watchit_core.config import settings


class _HeadRes:
    def __init__(self, action, confidence, risk="low"):
        self.action = action
        self.confidence = confidence
        self.risk = risk
        self.flags = ["f"]
        self.fast_scores = {"sexual": 0.0, "violence": 0.0, "profanity": 0.0}


class _Head:
    def __init__(self, res):
        self.res = res

    def run(self, event, profile):
        return self.res


class _UrlRes:
    def __init__(self, action, confidence, severity="low"):
        self.llm_decision = {
            "action": action,
            "severity": severity,
            "categories": [],
            "rationale": "r",
            "confidence": confidence,
        }
        self.confidence = confidence
        self.fast_scores = {"sexual": 0.0, "violence": 0.0, "profanity": 0.0}


class _Url:
    def __init__(self, res):
        self.res = res
        self.calls = 0

    def run(self, event, profile, extra_text="", fast_scores=None):
        self.calls += 1
        return self.res


class _Screens:
    def __init__(self, shots):
        self.shots = shots

    def get_screenshots(self, event):
        return self.shots


class _Ocr:
    def extract_text(self, shots):
        return "ocr text"


class _Policy:
    def run(self, state, profile):
        return {"action": (state.judge_json or {}).get("action", "allow"), "reason": "test", "categories": []}


def _run(monkeypatch, *, head, url, shots=None, enable_ocr=True, upgrade=False):
    fake_url = _Url(url)
    monkeypatch.setattr(g, "headlines_agent", _Head(head))
    monkeypatch.setattr(g, "url_agent", fake_url)
    monkeypatch.setattr(g, "screens_agent", _Screens(shots or []))
    monkeypatch.setattr(g, "ocr_agent", _Ocr())
    monkeypatch.setattr(g, "policy_agent", _Policy())
    monkeypatch.setattr(settings, "enable_ocr", enable_ocr)
    state = g.MonitorState(
        event={"url": "https://x.com", "title": "t"},
        child_profile={"strictness": "standard", "age": 12},
        is_upgrade=upgrade,
    )
    out = g.MonitorState(**g.app_graph.invoke(state))
    return out, fake_url.calls


def test_allowlist_confident_allow_skips_llm(monkeypatch):
    out, calls = _run(monkeypatch, head=_HeadRes("allow", 0.9), url=_UrlRes("allow", 0.9))
    assert out.final_decision["action"] == "allow"
    assert calls == 0


def test_high_risk_confident_block_skips_llm(monkeypatch):
    out, calls = _run(monkeypatch, head=_HeadRes("block", 0.9, risk="high"), url=_UrlRes("allow", 0.9))
    assert out.final_decision["action"] == "block"
    assert calls == 0


def test_uncertain_headline_escalates_to_llm(monkeypatch):
    out, calls = _run(monkeypatch, head=_HeadRes("allow", 0.55), url=_UrlRes("allow", 0.95))
    assert calls == 1
    assert out.final_decision["action"] == "allow"


def test_clearly_harmful_llm_blocks_without_ocr(monkeypatch):
    out, calls = _run(monkeypatch, head=_HeadRes("allow", 0.55), url=_UrlRes("block", 0.9, severity="high"), shots=["b64"])
    assert out.final_decision["action"] == "block"
    assert out.needs_screenshot is False
    assert calls == 1  # no OCR re-judge


def test_ambiguous_without_screenshot_defers_pending(monkeypatch):
    out, _ = _run(monkeypatch, head=_HeadRes("allow", 0.55), url=_UrlRes("warn", 0.6, severity="medium"))
    # No final decision: runtime emits the interim pending_ocr (warn), never allow.
    assert not out.final_decision
    assert out.needs_screenshot is True


def test_ocr_disabled_decides_immediately(monkeypatch):
    out, _ = _run(monkeypatch, head=_HeadRes("allow", 0.55), url=_UrlRes("warn", 0.6, severity="medium"), enable_ocr=False)
    assert out.final_decision["action"] == "warn"
    assert out.needs_screenshot is False


def test_ambiguous_with_screenshot_runs_ocr(monkeypatch):
    out, calls = _run(monkeypatch, head=_HeadRes("allow", 0.55), url=_UrlRes("warn", 0.6, severity="medium"), shots=["b64"])
    assert calls == 2  # pre-OCR judge + re-judge with OCR text
    assert out.final_decision["action"] == "warn"


def test_upgrade_enters_ocr_directly(monkeypatch):
    out, calls = _run(monkeypatch, head=_HeadRes("allow", 0.55), url=_UrlRes("allow", 0.95), shots=["b64"], upgrade=True)
    assert out.final_decision["action"] == "allow"
    assert calls == 1  # single judge inside the OCR node; headline/pre-url skipped


def test_judge_json_always_populated_before_policy(monkeypatch):
    # Every path that reaches policy must carry a judgment so the engine never
    # falls through to its default-allow branch.
    out, _ = _run(monkeypatch, head=_HeadRes("allow", 0.55), url=_UrlRes("blur", 0.95), shots=["b64"])
    assert out.judge_json.get("action")
