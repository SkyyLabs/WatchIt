from __future__ import annotations
from typing import Dict, Any
from urllib.parse import urlparse
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from watchit_core.config import settings

def _parse_time_range(spec: str) -> tuple[time, time]:
    # "21:00-07:00"
    a,b = spec.split("-")
    ah,am = map(int, a.split(":"))
    bh,bm = map(int, b.split(":"))
    return time(ah,am), time(bh,bm)

def _in_quiet_hours(now: datetime, days_csv: str, quiet_spec: str) -> bool:
    days = [d.strip() for d in days_csv.split(",") if d.strip()]
    dow = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][now.weekday()]
    if dow not in days:
        return False
    start, end = _parse_time_range(quiet_spec)
    t = now.time()
    if start <= end:
        return start <= t <= end
    else:
        # wraps midnight
        return not (end < t < start)

def _schedule_now(child_profile: Dict[str, Any] | None = None) -> datetime:
    timezone_name = (child_profile or {}).get("timezone")
    if not timezone_name:
        return datetime.now().astimezone()
    try:
        return datetime.now(ZoneInfo(str(timezone_name)))
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone()

STRICTNESS_THRESHOLDS = {
    "lenient": {"block": 0.95},
    "standard": {"block": 0.9},
    "strict": {"block": 0.8},
}


class PolicyEngine:
    def __init__(self):
        self.policy_version = settings.policy_version
        self.allow_domains = {"wikipedia.org", "khanacademy.org", ".edu"}
        self.block_domains = {"pornhub.com", "xvideos.com", "redtube.com"}

    def decide(
        self,
        event: Dict[str, Any],
        fast_scores: Dict[str, float],
        judge_json: Dict[str, Any],
        child_profile: Dict[str, Any] | None = None,
        headline_result: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        # Schedule in the child's configured timezone.
        now = _schedule_now(child_profile)
        if _in_quiet_hours(now, settings.sched_days, settings.sched_quiet):
            # During quiet hours, allow only educational domains; block others
            url = event.get("url") or ""
            dom = (urlparse(url).netloc or "").lower()
            if not any(a in dom for a in self.allow_domains):
                return {"action":"block", "reason":"schedule quiet hours", "categories":["schedule"]}

        url = event.get("url") or ""
        domain = (urlparse(url).netloc or "").lower()

        # allowlist first
        for a in self.allow_domains:
            if a in domain:
                return {"action":"allow","reason":f"allowlist {a}","categories":[]}

        # hard blocklist next
        for b in self.block_domains:
            if b in domain:
                return {"action":"block","reason":f"blocklist {b}","categories":["adult"]}

        # deterministic thresholds
        strictness = ((child_profile or {}).get("strictness") or "standard").lower()
        if strictness not in STRICTNESS_THRESHOLDS:
            strictness = "standard"
        thresholds = STRICTNESS_THRESHOLDS.get(strictness, STRICTNESS_THRESHOLDS["standard"])
        block_threshold = thresholds["block"]

        if (
            fast_scores.get("sexual",0)>=block_threshold
            or fast_scores.get("violence",0)>=block_threshold
            or fast_scores.get("profanity",0)>=block_threshold
        ):
            cats=[c for c in ("sexual","violence","profanity") if fast_scores.get(c,0)>=block_threshold]
            return {"action":"block","reason":"prefilter high","categories":cats}

        if headline_result:
            risk = headline_result.get("risk")
            if risk == "high":
                return {"action":"block","reason":"headline high risk", "categories":["headline"]}

        # LLM judge
        if judge_json:
            act = judge_json.get("action","allow")
            # allow/block/blur pass through; warn/notify are not enforced yet, so
            # anything else escalates to block.
            if act not in ("allow","block","blur"):
                act = "block"
            cats = judge_json.get("categories",[])
            sev  = judge_json.get("severity","low")
            reason = f"llm:{sev}"
            return {"action": act, "reason": reason, "categories": cats}

        return {"action":"allow","reason":"default allow","categories":[]}
