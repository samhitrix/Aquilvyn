"""OAuth2 / OIDC SSO (authorization-code flow + PKCE) against any spec-compliant IdP.

State, nonce and the PKCE verifier live in Redis for 10 minutes keyed by ``state``; the
ID token is verified against the provider's JWKS (signature, iss, aud, exp, nonce)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
import orjson

from fm_common.cache import cache
from fm_common.redis import get_redis

from .config import settings

STATE_TTL = 600


async def discovery() -> dict[str, Any]:
    async def load() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration")
            resp.raise_for_status()
            return resp.json()

    return await cache.get_or_load(f"oidc:discovery:{settings.oidc_issuer}", load, ttl=3600, l1_ttl=3600)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def build_authorize_url(platform: str = "web", redirect_after: str | None = None) -> str:
    meta = await discovery()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()
    await get_redis().set(
        f"fm:oidc:state:{state}",
        orjson.dumps({"nonce": nonce, "verifier": verifier, "platform": platform, "redirect": redirect_after}),
        ex=STATE_TTL,
    )
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": settings.oidc_scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{meta['authorization_endpoint']}?{urlencode(params)}"


async def complete_login(code: str, state: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (id_token_claims, stored_state)."""
    raw = await get_redis().getdel(f"fm:oidc:state:{state}")
    if raw is None:
        raise ValueError("Invalid or expired OIDC state")
    saved = orjson.loads(raw)
    meta = await discovery()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.oidc_redirect_uri,
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
                "code_verifier": saved["verifier"],
            },
        )
        resp.raise_for_status()
        tokens = resp.json()

    jwks_client = jwt.PyJWKClient(meta["jwks_uri"], cache_keys=True)
    # PyJWKClient fetches with blocking urllib: run it on a worker thread.
    signing_key = await asyncio.to_thread(jwks_client.get_signing_key_from_jwt, tokens["id_token"])
    claims = jwt.decode(
        tokens["id_token"],
        signing_key.key,
        algorithms=meta.get("id_token_signing_alg_values_supported", ["RS256"]),
        audience=settings.oidc_client_id,
        issuer=meta["issuer"],
    )
    if claims.get("nonce") != saved["nonce"]:
        raise ValueError("OIDC nonce mismatch")
    return claims, saved
