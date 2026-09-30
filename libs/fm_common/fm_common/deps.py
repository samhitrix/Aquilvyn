from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common.db.session import audit_ctx, get_db
from fm_common.identity.denylist import is_access_token_denied
from fm_common.identity.rbac import has_permission, permissions_for
from fm_common.identity.roles import Role
from fm_common.identity.tokens import AccessClaims, decode_access_token
from fm_common.logging import bind_context

bearer = HTTPBearer(auto_error=False)
DB = Annotated[AsyncSession, Depends(get_db)]


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    household_id: uuid.UUID
    role: Role
    jti: str
    claims: AccessClaims
    token: str  # forwarded on internal calls (on-behalf-of)

    def can(self, permission: str) -> bool:
        return has_permission(self.role, permission)

    @property
    def permissions(self) -> frozenset[str]:
        return permissions_for(self.role)

    @property
    def is_service(self) -> bool:
        return self.role == Role.SERVICE


def _unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


async def principal_from_token(token: str) -> Principal:
    try:
        claims = decode_access_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorized("Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("Invalid token") from exc
    if claims.typ == "access" and await is_access_token_denied(claims.jti):
        raise _unauthorized("Token revoked")
    principal = Principal(claims.sub, claims.household_id, Role(claims.role), claims.jti, claims, token)
    bind_context(user_id=str(principal.user_id), household_id=str(principal.household_id))
    ctx = dict(audit_ctx.get())
    ctx.update(user_id=str(principal.user_id), household_id=str(principal.household_id))
    audit_ctx.set(ctx)
    return principal


async def get_principal(
    request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise _unauthorized()
    principal = await principal_from_token(creds.credentials)
    request.state.principal = principal
    return principal


async def get_ws_principal(websocket: WebSocket) -> Principal:
    """Browsers can't set headers on WebSocket upgrades: accept ``?token=`` or a
    ``bearer.<jwt>`` subprotocol."""
    token = websocket.query_params.get("token")
    if not token:
        for proto in websocket.headers.get("sec-websocket-protocol", "").split(","):
            proto = proto.strip()
            if proto.startswith("bearer."):
                token = proto[7:]
    if not token:
        raise _unauthorized()
    return await principal_from_token(token)


CurrentUser = Annotated[Principal, Depends(get_principal)]


def require(*permissions: str):
    async def checker(principal: CurrentUser) -> Principal:
        missing = [p for p in permissions if not principal.can(p)]
        if missing:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing permission(s): {', '.join(missing)}")
        return principal

    return Depends(checker)
