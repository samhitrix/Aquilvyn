"""Resolve which AI providers a household uses: household settings (DB, keys encrypted with
AES-GCM) override the env defaults from .env. No provider => 'rules-only' mode."""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common.crypto import decrypt
from fm_common.logging import get_logger
from fm_common.redact import redact

from ..config import settings
from ..models import AIProvider
from .base import AIProviderClient

log = get_logger(__name__)
SUPPORTED = ("claude", "openai", "gemini", "groq", "cloudflare", "ollama")
GROQ_URL = "https://api.groq.com/openai/v1"
PAUSE_SECONDS = 24 * 3600  # a provider out of quota / rate-limited is skipped for a day


def clean(v: str | None) -> str | None:
    """Pasted keys / model names often carry tabs, spaces or newlines ('\tgemini-3-flash' → broken URL)."""
    v = "".join((v or "").split())
    return v or None


def make_client(provider: str, model: str | None, api_key: str | None, base_url: str | None) -> AIProviderClient | None:
    model, api_key, base_url = clean(model), clean(api_key), clean(base_url)
    if provider == "claude" and api_key:
        from .claude import ClaudeClient

        return ClaudeClient(api_key, model or "claude-opus-5")
    if provider == "openai" and api_key and model:
        from .openai_compat import OpenAICompatClient

        return OpenAICompatClient("openai", model, base_url or settings.openai_base_url, api_key)
    if provider == "gemini" and api_key and model:
        from .gemini import GeminiClient

        return GeminiClient(api_key, model)
    if provider == "groq" and api_key and model:
        from .openai_compat import OpenAICompatClient

        return OpenAICompatClient("groq", model, base_url or GROQ_URL, api_key)
    if provider == "cloudflare" and api_key and model and base_url:
        from .openai_compat import OpenAICompatClient

        # Workers AI's OpenAI-compatible endpoint; the settings store just the account ID
        url = base_url if base_url.startswith("http") else f"https://api.cloudflare.com/client/v4/accounts/{base_url}/ai/v1"
        return OpenAICompatClient("cloudflare", model, url, api_key)
    if provider == "ollama" and model:
        from .openai_compat import OpenAICompatClient

        return OpenAICompatClient("ollama", model, f"{(base_url or settings.ollama_base_url).rstrip('/')}/v1")
    return None


_OLLAMA_SEEN: dict[str, tuple[float, bool]] = {}


async def ollama_ready(base_url: str, model: str) -> bool:
    """A local Ollama only counts as a reviewer if it answers and has the model pulled — a
    pre-filled OLLAMA_MODEL with nothing running must not show up as 'active'. Cached 60 s."""
    key = f"{base_url}|{model}"
    hit = _OLLAMA_SEEN.get(key)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    ok = False
    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            r = await c.get(f"{base_url.rstrip('/')}/api/tags")
            names = {m.get("name", "") for m in r.json().get("models", [])} if r.status_code == 200 else set()
            ok = any(n == model or n.split(":")[0] == model.split(":")[0] for n in names)
    except Exception:
        ok = False
    _OLLAMA_SEEN[key] = (time.monotonic(), ok)
    return ok


def _env_defaults() -> list[dict[str, Any]]:
    return [
        {"provider": "claude", "model": settings.anthropic_model, "api_key": settings.anthropic_api_key, "base_url": None},
        {"provider": "openai", "model": settings.openai_model, "api_key": settings.openai_api_key, "base_url": settings.openai_base_url},
        {"provider": "gemini", "model": settings.gemini_model, "api_key": settings.gemini_api_key, "base_url": None},
        {"provider": "groq", "model": settings.groq_model, "api_key": settings.groq_api_key, "base_url": None},
        {"provider": "cloudflare", "model": settings.cloudflare_model, "api_key": settings.cloudflare_api_token, "base_url": settings.cloudflare_account_id},
        {"provider": "ollama", "model": settings.ollama_model, "api_key": None, "base_url": settings.ollama_base_url},
    ]


def _pause_key(household_id: uuid.UUID | str, provider: str) -> str:
    return f"fm:ai:pause:{household_id}:{provider}"


async def pause(household_id: uuid.UUID | str, provider: str, reason: str, seconds: int = PAUSE_SECONDS) -> None:
    """Skip this provider for a while (quota exhausted / rate-limited) — the next one in the order is used."""
    import orjson

    from fm_common.redis import get_redis

    until = time.time() + seconds
    await get_redis().set(_pause_key(household_id, provider), orjson.dumps({"until": until, "reason": reason[:300]}), ex=seconds)
    log.warning("ai.provider_paused", provider=provider, hours=round(seconds / 3600, 1), reason=reason[:120])


async def paused(household_id: uuid.UUID | str) -> dict[str, dict[str, Any]]:
    import orjson

    from fm_common.redis import get_redis

    r = get_redis()
    out: dict[str, dict[str, Any]] = {}
    for p in SUPPORTED:
        raw = await r.get(_pause_key(household_id, p))
        if raw:
            out[p] = orjson.loads(raw)
    return out


async def unpause(household_id: uuid.UUID | str, provider: str) -> None:
    from fm_common.redis import get_redis

    await get_redis().delete(_pause_key(household_id, provider))


PAUSE_MINUTE_SECONDS = 600  # a per-minute rate limit clears quickly: skip the provider for 10 minutes, not a day
_TOO_LARGE = ("request too large", "context length", "context_length", "maximum context", "reduce your message", "too many tokens",
              "payload too large", "prompt is too long", "input is too long")
_PER_MINUTE = ("per minute", "(tpm)", "(rpm)", "tokens per minute", "requests per minute", "try again in")
_DAILY = ("per day", "(tpd)", "(rpd)", "daily", "billing", "insufficient_quota", "current quota", "credit")


def error_kind(err: dict[str, Any]) -> str | None:
    """What a provider failure means for what we do next:
    ``too_large`` — the request was too big: retry with less evidence, don't pause the provider;
    ``rate_minute`` — a per-minute limit: pause briefly; ``quota`` — daily / billing quota: pause for a day."""
    text = str(err.get("error") or "").lower()
    status = err.get("status")
    if status == 413 or any(k in text for k in _TOO_LARGE):
        return "too_large"
    if status == 429 or "quota" in text or "resource_exhausted" in text or "rate limit" in text or "rate_limit" in text:
        if any(k in text for k in _DAILY):
            return "quota"
        return "rate_minute" if any(k in text for k in _PER_MINUTE) else "quota"
    return None


def pause_seconds(kind: str | None) -> int:
    return PAUSE_MINUTE_SECONDS if kind == "rate_minute" else PAUSE_SECONDS


def is_quota_error(err: dict[str, Any]) -> bool:
    return error_kind(err) in ("rate_minute", "quota")


async def household_clients(db: AsyncSession, household_id: uuid.UUID, include_paused: bool = False
                            ) -> tuple[list[AIProviderClient], AIProviderClient | None]:
    """Returns (chain, primary): enabled providers in the household's priority order — the first is used, the
    next ones only if it fails. Providers paused after a quota / rate-limit error are left out."""
    rows = {r.provider: r for r in (await db.execute(select(AIProvider).where(AIProvider.household_id == household_id))).scalars()}
    stopped = {} if include_paused else await paused(household_id)
    ranked: list[tuple[tuple[int, int], AIProviderClient]] = []
    for n, d in enumerate(_env_defaults()):
        row = rows.get(d["provider"])
        if d["provider"] in stopped:
            continue
        if row is not None:
            if not row.is_enabled:
                continue
            key = None
            if row.api_key_encrypted:
                try:
                    key = decrypt(row.api_key_encrypted, aad=f"{household_id}:{row.provider}")
                except Exception as exc:
                    log.warning("ai.key_decrypt_failed", provider=row.provider, error=str(exc))
                    continue
            client = make_client(row.provider, row.model, key or d["api_key"], row.base_url or d["base_url"])
            order = (row.priority if row.priority is not None else 100, n)
        else:
            client = make_client(d["provider"], d["model"], d["api_key"], d["base_url"])
            # .env providers come after everything saved in Settings — so the order shown there is the order used.
            # Only with nothing saved does AI_PRIMARY_PROVIDER pick which .env provider goes first.
            order = (1000 if rows else (0 if d["provider"] == settings.ai_primary_provider else 500), n)
        if client is None:
            continue
        if client.provider == "ollama" and not await ollama_ready((row.base_url if row is not None and row.base_url else None) or d["base_url"] or settings.ollama_base_url, client.model):
            continue
        ranked.append((order, client))
    chain = [c for _, c in sorted(ranked, key=lambda x: x[0])]
    return chain, (chain[0] if chain else None)


async def complete_with_fallback(household_id: uuid.UUID, chain: list[AIProviderClient], system: str, user: str,
                                 max_tokens: int = 1500) -> tuple[str | None, AIProviderClient | None, list[dict[str, Any]]]:
    """Try each provider in order; a quota / rate-limit error pauses that provider (10 min for a per-minute limit, else a day)."""
    errors: list[dict[str, Any]] = []
    for c in chain:
        try:
            return await c.complete(system, user, max_tokens), c, errors
        except Exception as exc:
            err = {"provider": c.provider, **describe_error(exc)}
            errors.append(err)
            if is_quota_error(err):
                await pause(household_id, c.provider, f"HTTP {err.get('status')}: {err['error']}", pause_seconds(error_kind(err)))
    return None, None, errors


def describe_error(exc: BaseException) -> dict[str, Any]:
    """A provider failure as something a person can act on: HTTP status + the provider's own message."""
    status = getattr(exc, "status_code", None)
    body: Any = None
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            body = exc.response.json()
        except Exception:
            body = exc.response.text
    elif getattr(exc, "body", None) is not None:  # anthropic.APIStatusError
        body = exc.body  # type: ignore[attr-defined]
    msg = None
    if isinstance(body, dict):
        err = body.get("error")
        msg = (err.get("message") if isinstance(err, dict) else err) or body.get("message")
    elif isinstance(body, str) and body.strip():
        msg = body.strip()[:300]
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        msg = "Timed out waiting for the provider"
    elif isinstance(exc, httpx.ConnectError):
        msg = f"Could not connect: {exc}"
    msg = redact(msg or exc or type(exc).__name__)[:400]
    low = msg.lower()
    if status == 400 and ("api key" in low or "api_key" in low):
        status_hint = "The API key was rejected — paste a fresh key in Settings → AI providers."
    elif status == 413 or any(k in low for k in _TOO_LARGE):
        status_hint = "The request was too large for this model's limit — retried with less evidence."
    else:
        status_hint = None
    hint = status_hint or {400: "Bad request — check the model name.", 401: "The API key was rejected.", 403: "The key has no access to this model / API.",
            404: "Model not found — check the model name.", 429: "Rate limit or quota exceeded.",
            }.get(status or 0) if status else None
    return {"status": status, "error": msg, "hint": hint or ("Provider-side error — retry later." if status and status >= 500 else None)}


async def test_client(client: AIProviderClient) -> dict[str, Any]:
    """Send one tiny real request, so a wrong key / model shows up when it's added — not later."""
    t0 = time.perf_counter()
    try:
        async with asyncio.timeout(45):
            reply = await client.complete("You are a connectivity check.", "Reply with the single word OK.", max_tokens=256)
        return {"ok": True, "status": 200, "provider": client.provider, "model": client.model,
                "latency_ms": int((time.perf_counter() - t0) * 1000), "reply": (reply or "").strip()[:60]}
    except Exception as exc:
        return {"ok": False, "provider": client.provider, "model": client.model, "latency_ms": int((time.perf_counter() - t0) * 1000),
                **describe_error(exc)}
