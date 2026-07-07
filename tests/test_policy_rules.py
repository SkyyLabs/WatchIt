"""Guardian manual rule evaluation: matching, precedence, expiry, suffix logic."""
import time

from watchit_core.policy.rules import domain_suffix_match, evaluate_rules


def _rule(**overrides):
    base = dict(
        id="rule_1", household_id="hh_1", child_id=None, device_id=None,
        action="block", rule_type="domain", pattern="roblox.com",
        expires_at=None, enabled=True,
    )
    base.update(overrides)
    return base


# --- suffix matching (gap B6: no substring false positives) -----------------

def test_domain_suffix_matches_domain_and_subdomains():
    assert domain_suffix_match("roblox.com", "roblox.com")
    assert domain_suffix_match("www.roblox.com".replace("www.", ""), "roblox.com")
    assert domain_suffix_match("play.roblox.com", "roblox.com")


def test_domain_suffix_rejects_substring_lookalikes():
    assert not domain_suffix_match("alphabet.com", "bet")
    assert not domain_suffix_match("evilroblox.com", "roblox.com")
    assert not domain_suffix_match("roblox.com.attacker.net", "roblox.com")


def test_dot_prefix_matches_tld_suffix_only():
    assert domain_suffix_match("mit.edu", ".edu")
    assert domain_suffix_match("cs.mit.edu", ".edu")
    assert not domain_suffix_match("malicious.education", ".edu")


# --- rule matching -----------------------------------------------------------

def test_domain_rule_blocks_subdomain():
    winning = evaluate_rules([_rule()], "https://play.roblox.com/games")
    assert winning and winning["action"] == "block"


def test_url_rule_matches_normalized_url_only():
    rule = _rule(rule_type="url", pattern="https://example.com/page?utm_source=x")
    assert evaluate_rules([rule], "https://www.example.com/page")
    assert evaluate_rules([rule], "https://example.com/other") is None


def test_prefix_rule_matches_path_prefix():
    rule = _rule(rule_type="prefix", pattern="https://example.com/games")
    assert evaluate_rules([rule], "https://example.com/games/123")
    assert evaluate_rules([rule], "https://example.com/homework") is None


def test_expired_rule_is_ignored():
    expired = _rule(expires_at=int(time.time() * 1000) - 1000)
    assert evaluate_rules([expired], "https://roblox.com/") is None


def test_no_url_no_match():
    assert evaluate_rules([_rule()], None) is None
    assert evaluate_rules([_rule()], "") is None


# --- precedence ---------------------------------------------------------------

def test_device_scope_beats_child_scope():
    child_block = _rule(id="r_child", child_id="c1", action="block")
    device_allow = _rule(id="r_dev", child_id="c1", device_id="d1", action="allow")
    winning = evaluate_rules([child_block, device_allow], "https://roblox.com/")
    assert winning["id"] == "r_dev"


def test_child_scope_beats_household_scope():
    household_allow = _rule(id="r_hh", action="allow")
    child_block = _rule(id="r_child", child_id="c1", action="block")
    winning = evaluate_rules([household_allow, child_block], "https://roblox.com/")
    assert winning["id"] == "r_child"


def test_block_beats_allow_at_equal_scope():
    allow = _rule(id="r_allow", action="allow")
    block = _rule(id="r_block", action="block")
    winning = evaluate_rules([allow, block], "https://roblox.com/")
    assert winning["id"] == "r_block"


def test_url_rule_beats_domain_rule_at_equal_scope():
    domain_block = _rule(id="r_domain", action="block", rule_type="domain", pattern="example.com")
    url_allow = _rule(id="r_url", action="allow", rule_type="url", pattern="https://example.com/school")
    winning = evaluate_rules([domain_block, url_allow], "https://example.com/school")
    assert winning["id"] == "r_url"
