"""Access tokens (JWT, 15 min), internal service tokens, and refresh-token primitives."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from fm_common.config import settings

SYSTEM_USER = uuid.UUID(int=0)


@dataclass(frozen=True, slots=True)
class AccessClaims:
    sub: uuid.UUID
    household_id: uuid.UUID
    role: str
    jti: str
    exp: datetime
    typ: str = "access"


def _encode(payload: dict, ttl: int) -> str:
    now = datetime.now(UTC)
    payload = {
        **payload,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(seconds=ttl),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: uuid.UUID, household_id: uuid.UUID, role: str) -> tuple[str, int]:
    ttl = settings.access_token_ttl_seconds
    return _encode({"sub": str(user_id), "hid": str(household_id), "role": role, "typ": "access"}, ttl), ttl


def create_service_token(service: str, household_id: uuid.UUID | None = None, ttl: int = 120) -> str:
    """Short-lived token a worker/service uses when no end-user token is being forwarded."""
    return _encode(
        {"sub": str(SYSTEM_USER), "hid": str(household_id or SYSTEM_USER), "role": "service", "typ": "service", "svc": service},
        ttl,
    )


def decode_access_token(token: str) -> AccessClaims:
    data = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        options={"require": ["exp", "sub", "hid", "role", "jti"]},
        leeway=10,
    )
    typ = data.get("typ")
    if typ not in ("access", "service"):
        raise jwt.InvalidTokenError("wrong token type")
    if typ == "service" and data["role"] != "service":
        raise jwt.InvalidTokenError("bad service token")
    return AccessClaims(
        sub=uuid.UUID(data["sub"]),
        household_id=uuid.UUID(data["hid"]),
        role=data["role"],
        jti=data["jti"],
        exp=datetime.fromtimestamp(data["exp"], UTC),
        typ=typ,
    )


def peek_subject(token: str) -> str | None:
    """Verified subject for rate-limit keying; None if the token is invalid. Signed service tokens (internal
    service-to-service calls) return "service" — they all share one system user, so keying them together would
    make every service throttle every other one."""
    try:
        claims = decode_access_token(token)
    except Exception:
        return None
    return "service" if claims.typ == "service" else str(claims.sub)


def new_refresh_token() -> tuple[str, str]:
    """Returns (plaintext, sha256-hex). Only the hash is persisted."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
