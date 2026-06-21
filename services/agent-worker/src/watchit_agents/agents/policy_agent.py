from __future__ import annotations

from typing import Any, Dict

from watchit_core.activity_logger import log_agent_step
from watchit_core.policy.engine import PolicyEngine


class PolicyAgent:
    """Wraps PolicyEngine.decide as an agent callable from the planner loop."""

    def __init__(self):
        self.engine = PolicyEngine()

    def run(self, state: Any, child_profile: Dict[str, Any]) -> Dict[str, Any]:
        decision = self.engine.decide(
            state.event,
            state.fast_scores,
            state.judge_json,
            child_profile,
            state.headline_result,
        )
        # Attach final decision back to state-compatible payload
        output = {
            "decision": decision,
        }
        log_agent_step(
            "PolicyAgent",
            "decide",
            getattr(state, "event", {}),
            {
                "fast_scores": state.fast_scores,
                "judge_json_present": bool(state.judge_json),
                "headline_result": state.headline_result,
            },
            output,
            {"loop_count": state.loop_count},
            f"policy action={decision.get('action')}",
        )
        return decision
