"""Safety failure policy: a judge outage or malformed model output must degrade
to warn (visible), never to silent allow, and must stay out of the URL cache
(confidence below the cache threshold)."""
from types import SimpleNamespace

from watchit_agents.llm_judge import LLMJudge
from watchit_core.config import settings
from watchit_core.policy.engine import PolicyEngine


def _judge():
    judge = LLMJudge.__new__(LLMJudge)  # skip __init__: no model client needed
    judge.model = "test-model"
    judge.logger = SimpleNamespace(
        debug=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, exception=lambda *a, **k: None,
    )
    judge._guardian_cache = None
    return judge


def _run(judge):
    return judge.judge(
        page_title="t", domain="example.com", fast_scores={}, text_sample="",
        child_age=12, strictness="standard",
    )


def test_llm_call_failure_degrades_to_warn(monkeypatch):
    judge = _judge()
    judge.client = SimpleNamespace(invoke=lambda _m: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(LLMJudge, "_guardian_guidance", lambda self: None)
    result = _run(judge)
    assert result["action"] == "warn"
    assert "system_uncertain" in result["categories"]
    assert result["confidence"] < settings.url_decision_cache_min_confidence


def test_llm_garbage_output_degrades_to_warn(monkeypatch):
    judge = _judge()
    judge.client = SimpleNamespace(invoke=lambda _m: SimpleNamespace(content="not json at all"))
    monkeypatch.setattr(LLMJudge, "_guardian_guidance", lambda self: None)
    result = _run(judge)
    assert result["action"] == "warn"
    assert "system_uncertain" in result["categories"]
    assert result["confidence"] < settings.url_decision_cache_min_confidence


def test_policy_engine_keeps_system_uncertain_as_warn():
    # warn is normally coerced to block; the system_uncertain degradation must
    # stay warn (blur + banner) — neither silent allow nor a hard block of the
    # whole web during an LLM outage.
    judge_json = {"action": "warn", "categories": ["system_uncertain"], "severity": "medium"}
    decision = PolicyEngine().decide({"url": "https://example.com/x"}, {}, judge_json)
    assert decision["action"] == "warn"
    assert decision["reason"] == "system_uncertain"
