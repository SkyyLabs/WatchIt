"""Headline layer domain matching: no substring false positives (gap B6) —
"alphabet.com" must not high-risk-block on the "bet" token, and lookalike
domains must not ride the allowlist."""
from watchit_agents.agents.headlines_agent import HeadlinesAgent, _domain_has_token


def _run(url, title=""):
    return HeadlinesAgent().run({"url": url, "title": title, "data_json": None}, {"strictness": "standard"})


def test_token_label_matching():
    assert _domain_has_token("pornhub.com", "porn")
    assert _domain_has_token("bet365.com", "bet")
    assert not _domain_has_token("alphabet.com", "bet")
    assert not _domain_has_token("scunthorpe.gov.uk", "porn")


def test_alphabet_com_is_not_high_risk():
    result = _run("https://alphabet.com/investors")
    assert result.action != "block"


def test_porn_domain_still_blocks():
    result = _run("https://pornhub.com/")
    assert result.action == "block"
    assert result.confidence >= 0.85


def test_lookalike_domain_not_allowlisted():
    # substring matching used to treat any host containing "wikipedia.org" as low
    # risk; suffix matching must not.
    trusted = _run("https://en.wikipedia.org/wiki/Math")
    lookalike = _run("https://wikipedia.org.attacker.net/")
    assert "headline_low_risk" in trusted.flags
    assert "headline_low_risk" not in lookalike.flags
