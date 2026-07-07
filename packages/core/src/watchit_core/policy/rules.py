"""Guardian manual allow/block rule evaluation.

One shared implementation used by the worker pipeline (authoritative) and the
policy snapshot; the extension mirrors this logic in JS for local enforcement.

Precedence (first match after sorting wins):
1. Scope specificity: device rule > child rule > household rule.
2. Pattern specificity within a scope: url > prefix > domain.
3. Action within equal specificity: block beats allow.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from watchit_core.url_cache import normalize_url

# Shared with the headline layer and the policy snapshot so the extension can
# flag high-risk unknown pages locally.
HIGH_RISK_TOKENS = ["porn", "xxx", "casino", "bet", "nsfw", "escort"]


def domain_suffix_match(host: str, pattern: str) -> bool:
    """Exact-suffix domain match: `pattern` matches itself and its subdomains.

    A leading dot means "any registrable domain under this suffix" (".edu").
    Substring matching is deliberately not used — see gap analysis B6.
    """
    host = (host or "").lower().strip(".")
    pattern = (pattern or "").lower().strip()
    if not host or not pattern:
        return False
    if pattern.startswith("."):
        return host == pattern[1:] or host.endswith(pattern)
    return host == pattern or host.endswith("." + pattern)


def host_of(url: str | None) -> str:
    try:
        host = urlsplit(url or "").netloc.lower()
    except Exception:
        return ""
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


_TYPE_SPECIFICITY = {"url": 3, "prefix": 2, "domain": 1}


def _scope_specificity(rule: Dict[str, Any]) -> int:
    if rule.get("device_id"):
        return 3
    if rule.get("child_id"):
        return 2
    return 1


def _rule_matches(rule: Dict[str, Any], url: str, normalized: str, host: str) -> bool:
    rule_type = rule.get("rule_type")
    pattern = rule.get("pattern") or ""
    if rule_type == "domain":
        return domain_suffix_match(host, pattern)
    if rule_type == "url":
        return normalized == normalize_url(pattern)
    if rule_type == "prefix":
        return normalized.startswith(normalize_url(pattern))
    return False


def _expired(rule: Dict[str, Any]) -> bool:
    expires_at = rule.get("expires_at")
    if not expires_at:
        return False
    if isinstance(expires_at, (int, float)):
        return expires_at <= datetime.now(timezone.utc).timestamp() * 1000
    if isinstance(expires_at, datetime):
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= datetime.now(timezone.utc)
    return False


def evaluate_rules(rules: List[Dict[str, Any]], url: str | None) -> Optional[Dict[str, Any]]:
    """Return the winning rule for `url`, or None.

    `rules` must already be scoped to the household/child/device (the repository
    query does that); this only handles matching, expiry, and precedence.
    """
    if not url:
        return None
    normalized = normalize_url(url)
    host = host_of(url)
    candidates = [
        rule
        for rule in rules
        if rule.get("enabled", True)
        and rule.get("action") in ("allow", "block")
        and not _expired(rule)
        and _rule_matches(rule, url, normalized, host)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda rule: (
            _scope_specificity(rule),
            _TYPE_SPECIFICITY.get(rule.get("rule_type"), 0),
            1 if rule.get("action") == "block" else 0,
        ),
        reverse=True,
    )
    return candidates[0]
