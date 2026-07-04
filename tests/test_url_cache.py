"""URL normalization + cache-key determinism (packages/core url_cache)."""
from watchit_core.url_cache import normalize_url, url_cache_key


def test_normalize_strips_www_and_tracking_params():
    assert normalize_url("https://www.Example.com/Path/?utm_source=x&a=1") == "https://example.com/Path?a=1"


def test_normalize_sorts_query_and_trims_trailing_slash():
    assert normalize_url("https://example.com/p/?b=2&a=1") == "https://example.com/p?a=1&b=2"


def test_normalize_empty_and_none():
    assert normalize_url(None) == ""
    assert normalize_url("") == ""


def test_normalize_root_path_kept():
    assert normalize_url("https://example.com") == "https://example.com/"


def test_cache_key_is_deterministic():
    args = dict(url="https://www.example.com/x?utm_medium=y", child_id="c1", strictness="strict", age=10, policy_version="1.0.0")
    norm1, key1 = url_cache_key(**args)
    norm2, key2 = url_cache_key(**args)
    assert key1 == key2
    assert norm1 == "https://example.com/x"


def test_cache_key_varies_with_scope():
    base = dict(url="https://example.com/x", strictness="standard", age=12, policy_version="1.0.0")
    _, k_child_a = url_cache_key(child_id="a", **base)
    _, k_child_b = url_cache_key(child_id="b", **base)
    assert k_child_a != k_child_b
