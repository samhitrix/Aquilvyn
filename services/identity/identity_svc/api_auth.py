from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

import orjson
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from fm_common.deps import DB, CurrentUser
from fm_common.events import publish
from fm_common.identity.denylist import deny_access_token
from fm_common.identity.rbac import permissions_for
from fm_common.logging import get_logger
from fm_common.ratelimit.middleware import client_ip
from fm_common.redis import get_redis

from . import oidc
from .config import settings
from .models import User
from .passwords import hash_password, needs_rehash, verify_password
from .tokens_service import (
    AuthError,
    ClientInfo,
    TokenPair,
    create_household_with_owner,
    issue_tokens,
    revoke_all_for_user,
    revoke_refresh_token,
    rotate_refresh_token,
)

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)
Platform = Literal["web", "ios", "android"]
NATIVE = {"ios", "android"}


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    full_name: str = Field(default="", max_length=200)
    household_name: str | None = Field(default=None, max_length=200)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)
    device_id: str | None = None


class RefreshIn(BaseModel):
    refresh_token: str | None = None  # native clients only; web uses the HttpOnly cookie
    device_id: str | None = None


class UserOut(BaseModel):
    id: uuid.UUID
    household_id: uuid.UUID
    household_name: str
    email: str
    full_name: str
    role: str
    permissions: list[str]
    has_password: bool = True  # False: signs in with Gmail only (no password to change)
    has_recovery_code: bool = False


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str | None = None  # only for native platforms (stored in Keychain/Keystore)
    refresh_expires_at: datetime | None = None
    user: UserOut
    recovery_code: str | None = None  # shown once, at sign-up: the way back in if the password is forgotten


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id, household_id=user.household_id, household_name=user.household.name,
        email=user.email, full_name=user.full_name,
        role=user.role.value, permissions=sorted(permissions_for(user.role)), has_password=bool(user.password_hash),
        has_recovery_code=bool(user.recovery_hash),
    )


async def _announce(user: User) -> None:
    """portfolio-svc listens and bootstraps a 'Self' profile + default portfolio."""
    await publish(
        "member.registered",
        {"user_id": str(user.id), "full_name": user.full_name, "email": user.email, "role": user.role.value},
        household_id=str(user.household_id),
    )


def _client(request: Request, platform: str, device_id: str | None = None) -> ClientInfo:
    return ClientInfo(
        platform=platform, device_id=device_id,
        user_agent=request.headers.get("user-agent"), ip=client_ip(request.scope),
    )


def _set_refresh_cookie(response: Response, pair: TokenPair) -> None:
    response.set_cookie(
        settings.refresh_cookie_name,
        pair.refresh_token,
        max_age=settings.refresh_token_ttl_days * 86400,
        expires=pair.refresh_expires_at,
        path=settings.refresh_cookie_path,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )


def _respond(response: Response, pair: TokenPair, platform: str) -> TokenOut:
    out = TokenOut(access_token=pair.access_token, expires_in=pair.expires_in, user=_user_out(pair.user))
    if platform in NATIVE:
        out.refresh_token = pair.refresh_token
        out.refresh_expires_at = pair.refresh_expires_at
    else:
        _set_refresh_cookie(response, pair)
    response.headers["Cache-Control"] = "no-store"
    return out


@router.post("/register", response_model=TokenOut, status_code=201)
async def register(
    body: RegisterIn, request: Request, response: Response, db: DB,
    x_client_platform: Platform = Header(default="web"),
) -> TokenOut:
    exists = await db.scalar(select(User.id).where(func.lower(User.email) == body.email.lower()))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user = await create_household_with_owner(
        db, email=body.email, full_name=body.full_name, password=body.password, household_name=body.household_name
    )
    code = new_recovery_code()
    user.recovery_hash = await hash_password(normalise_code(code))
    pair = await issue_tokens(db, user, _client(request, x_client_platform))
    await db.commit()
    await db.refresh(user, ["household"])
    await _announce(user)
    log.info("auth.registered", email=user.email)
    out = _respond(response, pair, x_client_platform)
    out.recovery_code = code
    return out


@router.post("/login", response_model=TokenOut)
async def login(
    body: LoginIn, request: Request, response: Response, db: DB,
    x_client_platform: Platform = Header(default="web"),
) -> TokenOut:
    user = (await db.execute(select(User).where(func.lower(User.email) == body.email.lower()))).scalar_one_or_none()
    ok = await verify_password(body.password, user.password_hash if user else None)
    if not user or not ok or not user.is_active:
        log.warning("auth.login_failed", email=body.email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if user.password_hash and needs_rehash(user.password_hash):
        user.password_hash = await hash_password(body.password)
    code = None
    if not user.recovery_hash:  # made before recovery codes existed: make one now and show it once
        code = new_recovery_code()
        user.recovery_hash = await hash_password(normalise_code(code))
        log.info("auth.recovery_code_created", on="login")
    user.last_login_at = datetime.now(UTC)
    pair = await issue_tokens(db, user, _client(request, x_client_platform, body.device_id))
    await db.commit()
    log.info("auth.login", platform=x_client_platform)
    out = _respond(response, pair, x_client_platform)
    out.recovery_code = code
    return out


@router.post("/refresh", response_model=TokenOut)
async def refresh(
    request: Request, response: Response, db: DB, body: RefreshIn | None = None,
    x_client_platform: Platform = Header(default="web"),
) -> TokenOut:
    raw = (body.refresh_token if body else None) if x_client_platform in NATIVE else request.cookies.get(settings.refresh_cookie_name)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing refresh token")
    try:
        pair = await rotate_refresh_token(db, raw, _client(request, x_client_platform, body.device_id if body else None))
    except AuthError as exc:
        response.delete_cookie(settings.refresh_cookie_name, path=settings.refresh_cookie_path)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc), headers={"X-Auth-Error": exc.code}) from exc
    await db.commit()
    return _respond(response, pair, x_client_platform)


@router.post("/logout", status_code=204)
async def logout(
    request: Request, response: Response, principal: CurrentUser, db: DB, body: RefreshIn | None = None,
    all_devices: bool = False,
) -> Response:
    raw = (body.refresh_token if body else None) or request.cookies.get(settings.refresh_cookie_name)
    if all_devices:
        await revoke_all_for_user(db, principal.user_id)
    elif raw:
        await revoke_refresh_token(db, raw)
    await db.commit()
    await deny_access_token(principal.jti, principal.claims.exp)
    response = Response(status_code=204)
    response.delete_cookie(settings.refresh_cookie_name, path=settings.refresh_cookie_path)
    return response


@router.get("/me", response_model=UserOut)
async def me(principal: CurrentUser, db: DB) -> UserOut:
    user = await db.get(User, principal.user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    return _user_out(user)


class MePatch(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)


@router.patch("/me", response_model=UserOut)
async def update_me(body: MePatch, principal: CurrentUser, db: DB) -> UserOut:
    """Rename yourself (kept in sync with your own 'Self' profile)."""
    user = await db.get(User, principal.user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    user.full_name = body.full_name.strip()
    await db.commit()
    return _user_out(user)


# ---------------- Passwords ----------------
class PasswordIn(BaseModel):
    new_password: str = Field(min_length=10, max_length=128)
    current_password: str = Field(default="", max_length=128)  # not asked when the account has no password yet


@router.post("/password")
async def change_password(body: PasswordIn, principal: CurrentUser, db: DB) -> dict[str, str]:
    """Change your password — other devices are signed out. An account with no password yet (e.g. you forgot it and
    have been signing in with Gmail) sets one here: you are already signed in, so there is no old one to ask for.
    Setting it also makes a recovery code (returned once) when the account has none."""
    user = await db.get(User, principal.user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    first = not user.password_hash
    if not first:
        if not body.current_password:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Enter your current password")
        if not await verify_password(body.current_password, user.password_hash):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is wrong")
    user.password_hash = await hash_password(body.new_password)
    out: dict[str, str] = {}
    if not user.recovery_hash:
        code = new_recovery_code()
        user.recovery_hash = await hash_password(normalise_code(code))
        out["recovery_code"] = code
    if not first:
        await revoke_all_for_user(db, user.id)
    await db.commit()
    log.info("auth.password_set" if first else "auth.password_changed")
    return out


# ---- Recovery code: "Forgot password?" without an email server ----
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I: easy to read back from paper


def new_recovery_code() -> str:
    """16 characters (80 bits) in 4 groups: K7QH-9ZPM-4XWD-T2NB."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def normalise_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())  # dashes / spaces / lower case don't matter


class RecoveryCodeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)


@router.post("/recovery-code")
async def create_recovery_code(body: RecoveryCodeIn, principal: CurrentUser, db: DB) -> dict[str, str]:
    """Create (or replace) your recovery code — shown once. Needs your current password."""
    user = await db.get(User, principal.user_id)
    if user is None or not user.password_hash:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This account signs in with Gmail and has no password to recover.")
    if not await verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is wrong")
    code = new_recovery_code()
    user.recovery_hash = await hash_password(normalise_code(code))
    await db.commit()
    log.info("auth.recovery_code_created")
    return {"recovery_code": code}


class RecoverIn(BaseModel):
    email: EmailStr
    recovery_code: str = Field(min_length=8, max_length=40)
    new_password: str = Field(min_length=10, max_length=128)


@router.post("/password/recover")
async def recover_password(body: RecoverIn, db: DB) -> dict[str, str]:
    """Forgot password: email + recovery code → new password. The code is used up; a new one is returned (shown
    once). Same answer for a wrong email or a wrong code; rate-limited per IP."""
    user = (await db.execute(select(User).where(func.lower(User.email) == body.email.lower()))).scalar_one_or_none()
    ok = await verify_password(normalise_code(body.recovery_code), user.recovery_hash if user else None)
    if not user or not ok or not user.is_active or not user.password_hash:
        log.warning("auth.recover_failed")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That email and recovery code don't match.")
    code = new_recovery_code()
    user.password_hash = await hash_password(body.new_password)
    user.recovery_hash = await hash_password(normalise_code(code))
    await revoke_all_for_user(db, user.id)
    await db.commit()
    log.info("auth.password_recovered")
    return {"recovery_code": code}


RESET_KEY = "fm:auth:reset:"  # + sha256(token) → user id, 30 minutes, used once


async def make_reset_link(db: Any, email: str, minutes: int = 30) -> str | None:
    """A one-time password-reset link — made on the machine that runs Aquilvyn (``fm.py reset-password``) or by the
    household owner / an admin for a member (Settings → Household members), since a local install has no email
    server to send one."""
    user = (await db.execute(select(User).where(func.lower(User.email) == email.lower()))).scalar_one_or_none()
    if user is None or not user.password_hash:  # Gmail-only accounts sign in with Google, not a password
        return None
    token = secrets.token_urlsafe(32)
    await get_redis().set(RESET_KEY + hashlib.sha256(token.encode()).hexdigest(), str(user.id), ex=minutes * 60)
    return settings.login_page.rsplit("/", 1)[0] + "/reset-password?token=" + token


class ResetIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=10, max_length=128)


@router.post("/password/reset", status_code=204)
async def reset_password(body: ResetIn, db: DB) -> Response:
    key = RESET_KEY + hashlib.sha256(body.token.encode()).hexdigest()
    uid = await get_redis().getdel(key)
    user = await db.get(User, uuid.UUID(uid.decode() if isinstance(uid, bytes) else uid)) if uid else None
    if user is None or not user.is_active or not user.password_hash:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This reset link has expired or was already used — ask for a new one.")
    user.password_hash = await hash_password(body.new_password)
    await revoke_all_for_user(db, user.id)
    await db.commit()
    log.info("auth.password_reset")
    return Response(status_code=204)


# ---------------- OIDC SSO ----------------
def _back_to_login(message: str) -> RedirectResponse:
    """Every Gmail-login problem lands on the login page with a readable reason — never a bare JSON error."""
    return RedirectResponse(f"{settings.login_page}?error=" + quote(message), 302)


@router.get("/sso")
async def sso_status() -> dict[str, Any]:
    """Whether "Login with Gmail" is set up (the login page shows the button either way and explains what's missing)."""
    return {"enabled": settings.sso_ready, "provider": settings.oidc_provider_name}


@router.get("/oidc/login")
async def oidc_login(platform: Platform = "web") -> RedirectResponse:
    if not settings.sso_ready:
        return _back_to_login("Login with Gmail isn't set up yet: add OIDC_CLIENT_ID and OIDC_CLIENT_SECRET (Google Cloud → APIs & Services → "
                              "Credentials → OAuth client) to .env, then run python scripts/fm.py up-lite.")
    try:
        url = await oidc.build_authorize_url(platform=platform)
    except Exception as exc:  # e.g. the IdP can't be reached — show why instead of a bare 500
        log.warning("auth.oidc_discovery_failed", error=str(exc)[:200])
        return _back_to_login(f"Couldn't reach Google ({settings.oidc_issuer}): {str(exc)[:120]}")
    return RedirectResponse(url, status_code=302)


@router.get("/oidc/callback")
async def oidc_callback(request: Request, db: DB, code: str | None = None, state: str | None = None, error: str | None = None) -> Response:
    if not settings.sso_ready:
        return _back_to_login("Login with Gmail isn't set up on this server.")
    if error or not code or not state:  # e.g. the person pressed Cancel on Google's screen
        return _back_to_login("Gmail login was cancelled." if error == "access_denied" else f"Google didn't complete the login ({error or 'no code returned'}).")
    try:
        claims, saved = await oidc.complete_login(code, state)
    except Exception as exc:
        log.warning("auth.oidc_failed", error=str(exc))
        return _back_to_login("Gmail login failed — the login link may have expired. Please try again. "
                              "If it keeps failing, check that OIDC_REDIRECT_URI in .env exactly matches the one in Google Cloud.")

    provider, subject = settings.oidc_provider_name, claims["sub"]
    email = (claims.get("email") or "").lower()
    user = (
        await db.execute(select(User).where(User.oidc_provider == provider, User.oidc_subject == subject))
    ).scalar_one_or_none()
    if user is None and email and claims.get("email_verified"):
        # Link to an existing password account only when the IdP vouches for the email.
        user = (await db.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
        if user:
            user.oidc_provider, user.oidc_subject = provider, subject
    new_user = user is None
    if user is None:
        if not email:
            return _back_to_login("Google didn't share your email address, so no account could be created.")
        user = await create_household_with_owner(
            db, email=email, full_name=claims.get("name", ""), password=None,
            oidc_provider=provider, oidc_subject=subject, email_verified=bool(claims.get("email_verified")),
        )
        await db.flush()
        await _announce(user)
    user.last_login_at = datetime.now(UTC)

    platform = saved.get("platform", "web")
    if platform in NATIVE:
        # Hand the native app a one-time code via deep link; it exchanges it over TLS for tokens.
        one_time = secrets.token_urlsafe(32)
        await get_redis().set(f"fm:oidc:otc:{one_time}", orjson.dumps({"uid": str(user.id), "platform": platform}), ex=60)
        await db.commit()
        return RedirectResponse(f"aquilvyn://auth/callback?code={one_time}", status_code=302)

    pair = await issue_tokens(db, user, _client(request, "web"))
    await db.commit()
    target = saved.get("redirect") or settings.oidc_post_login_redirect
    if new_user:  # Google gives a name and email only — ask for PAN, date of birth, tax slab … first
        target = settings.oidc_post_login_redirect.rsplit("/", 1)[0] + "/welcome"
    redirect = RedirectResponse(target, status_code=302)
    _set_refresh_cookie(redirect, pair)
    return redirect


class OneTimeCodeIn(BaseModel):
    code: str
    device_id: str | None = None


@router.post("/oidc/exchange", response_model=TokenOut)
async def oidc_exchange(body: OneTimeCodeIn, request: Request, response: Response, db: DB) -> TokenOut:
    raw = await get_redis().getdel(f"fm:oidc:otc:{body.code}")
    if raw is None:
        raise HTTPException(400, "Invalid or expired code")
    data = orjson.loads(raw)
    user = await db.get(User, uuid.UUID(data["uid"]))
    if user is None or not user.is_active:
        raise HTTPException(401, "User disabled")
    pair = await issue_tokens(db, user, _client(request, data["platform"], body.device_id))
    await db.commit()
    return _respond(response, pair, data["platform"])
