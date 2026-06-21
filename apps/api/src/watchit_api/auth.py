from __future__ import annotations

from typing import Any, Dict

import jwt
from fastapi import Header, HTTPException, Query

from watchit_core.config import settings
from watchit_core.logging import bind_log_context, get_logger

_jwks_client: jwt.PyJWKClient | None = None
logger = get_logger("watchit.auth")


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


async def require_guardian(authorization: str | None = Header(default=None)) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        logger.warning("guardian_auth_missing")
        raise HTTPException(401, "Missing bearer token")
    return verify_clerk_token(authorization.split(" ", 1)[1].strip())


async def require_guardian_stream(token: str | None = Query(default=None)) -> Dict[str, Any]:
    if not token:
        logger.warning("guardian_stream_auth_missing")
        raise HTTPException(401, "Missing stream token")
    return verify_clerk_token(token)
