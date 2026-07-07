from __future__ import annotations

import threading
import time
from typing import Any, Dict

import jwt
from fastapi import Header, HTTPException, Query

from watchit_core.config import settings
from watchit_core.db import db
from watchit_core.logging import bind_log_context, get_logger

_jwks_client: jwt.PyJWKClient | None = None
logger = get_logger("watchit.auth")

# ensure_guardian_household() writes (last_seen upsert) and resolves the household
# on every call. Cache the resolved {guardian, household} per Clerk subject so the
# common case skips the DB entirely; TTL bounds staleness for membership changes.
_HOUSEHOLD_CACHE_TTL_SECONDS = 300
_household_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
_household_cache_lock = threading.Lock()


def _resolve_household(claims: Dict[str, Any]) -> Dict[str, Any]:
    subject = claims.get("sub")
    now = time.monotonic()
    if subject:
        with _household_cache_lock:
            cached = _household_cache.get(subject)
            if cached and cached[0] > now:
                return cached[1]
    context = db.ensure_guardian_household(claims)
    if subject:
        with _household_cache_lock:
            _household_cache[subject] = (now + _HOUSEHOLD_CACHE_TTL_SECONDS, context)
    return context


def _select_active_household(context: Dict[str, Any], requested_id: str | None) -> Dict[str, Any]:
    # The dashboard picks the active household via X-Household-Id (or the `household`
    # stream query param). Validate it against the guardian's memberships every
    # request — never trust a client-supplied id. Unknown/unowned → default household.
    default_household = context["household"]
    if not requested_id or requested_id == default_household.get("id"):
        return context
    households = db.list_guardian_households(context["guardian"]["id"])
    match = next((h for h in households if h["id"] == requested_id), None)
    if not match:
        return context
    return {**context, "household": dict(match)}


def _jwks_client_for_settings() -> jwt.PyJWKClient:
    global _jwks_client
    jwks_url = settings.clerk_jwks_url
    if not jwks_url and settings.clerk_issuer:
        jwks_url = settings.clerk_issuer.rstrip("/") + "/.well-known/jwks.json"
    if not jwks_url:
        raise HTTPException(500, "CLERK_JWKS_URL is not configured")
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(jwks_url)
    return _jwks_client


def verify_clerk_token(token: str) -> Dict[str, Any]:
    try:
        signing_key = _jwks_client_for_settings().get_signing_key_from_jwt(token)
        options = {"verify_aud": False}
        kwargs: Dict[str, Any] = {"algorithms": ["RS256"], "options": options}
        if settings.clerk_issuer:
            kwargs["issuer"] = settings.clerk_issuer
        claims = jwt.decode(token, signing_key.key, **kwargs)
        bind_log_context(guardian_subject=claims.get("sub"), clerk_session_id=claims.get("sid"))
        return claims
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("clerk_token_invalid")
        raise HTTPException(401, "Invalid Clerk token") from exc


# Sync def so FastAPI runs it (and its blocking DB work on cache miss) in a
# threadpool instead of on the event loop, keeping read endpoints concurrent.
def require_guardian(
    authorization: str | None = Header(default=None),
    x_household_id: str | None = Header(default=None, alias="X-Household-Id"),
) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        logger.warning("guardian_auth_missing")
        raise HTTPException(401, "Missing bearer token")
    claims = verify_clerk_token(authorization.split(" ", 1)[1].strip())
    context = _select_active_household(_resolve_household(claims), x_household_id)
    bind_log_context(
        guardian_id=context["guardian"].get("id"),
        household_id=context["household"].get("id"),
    )
    return {**claims, **context}


async def require_guardian_stream(
    token: str | None = Query(default=None),
    household: str | None = Query(default=None),
) -> Dict[str, Any]:
    if not token:
        logger.warning("guardian_stream_auth_missing")
        raise HTTPException(401, "Missing stream token")
    claims = verify_clerk_token(token)
    context = _select_active_household(_resolve_household(claims), household)
    bind_log_context(
        guardian_id=context["guardian"].get("id"),
        household_id=context["household"].get("id"),
    )
    return {**claims, **context}


def _resolve_device_or_401(token: str) -> Dict[str, Any]:
    # Structured 401 codes let the extension distinguish "re-pair" (device_revoked)
    # from "rotate" (token_expired) from "unknown token" (invalid).
    device, error = db.resolve_device_token(token)
    if error:
        logger.warning("device_auth_invalid", auth_error=error)
        raise HTTPException(401, {"code": error, "message": "Device token rejected"})
    bind_log_context(
        device_id=device.get("id"),
        child_id=device.get("child_id"),
        household_id=device.get("household_id"),
    )
    return {"device": device}


async def require_device_stream(token: str | None = Query(default=None)) -> Dict[str, Any]:
    # EventSource cannot set an Authorization header, so the extension passes its
    # device token as a query param. Same scoping as require_device.
    if not token:
        logger.warning("device_stream_auth_missing")
        raise HTTPException(401, "Missing stream token")
    return _resolve_device_or_401(token)


async def require_device(authorization: str | None = Header(default=None)) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        logger.warning("device_auth_missing")
        raise HTTPException(401, "Missing device token")
    return _resolve_device_or_401(authorization.split(" ", 1)[1].strip())
