from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "gbraid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}


def normalize_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except Exception:
        return (url or "").strip().lower()
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_pairs), doseq=True)
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, query, ""))


def url_cache_key(
    *,
    url: str | None,
    child_id: str | None,
    strictness: str | None,
    age: int | None,
    policy_version: str,
) -> tuple[str, str]:
    normalized_url = normalize_url(url)
    material = "|".join(
        [
            normalized_url,
            child_id or "",
            strictness or "standard",
            str(age or 12),
            policy_version,
        ]
    )
    return normalized_url, hashlib.sha256(material.encode("utf-8")).hexdigest()
