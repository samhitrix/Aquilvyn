"""OpenAI-compatible chat-completions adapter — used for OpenAI and for local Ollama
(which serves the same ``/v1/chat/completions`` API)."""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

from .base import VERDICT_SCHEMA, Verdict, packet_text, parse_verdict, system_for

AUTO = "auto"
NOT_CHAT = re.compile(r"(?i)whisper|tts|speech|audio|guard|embed|rerank|moderation|vision-preview|playai|orpheus|transcri|image|ocr")
PREFER = ("gpt-oss-120b", "llama-4-maverick", "llama-3.3-70b", "qwen3-32b", "kimi-k2", "deepseek", "gpt-oss-20b", "llama-4-scout", "llama")
_picked: dict[str, tuple[float, str]] = {}  # provider|base → (when, model) — a retired model is replaced for a day


def pick_model(ids: list[str]) -> str | None:
    """The strongest general chat model in a provider's /models list (names change as providers retire models)."""
    chat = [i for i in ids if not NOT_CHAT.search(i)]
    for p in PREFER:
        hits = sorted((i for i in chat if p in i.lower()), key=len)
        if hits:
            return hits[0]

    def size(i: str) -> float:
        m = re.findall(r"(\d+(?:\.\d+)?)b\b", i.lower())
        return max((float(x) for x in m), default=0.0)
    return max(chat, key=size) if chat else None


def model_missing(r: httpx.Response) -> bool:
    if r.status_code not in (400, 404):
        return False
    t = r.text.lower()
    return "model" in t and any(w in t for w in ("not exist", "not found", "decommissioned", "deprecated", "no longer", "invalid model", "unknown model"))


class OpenAICompatClient:
    def __init__(self, provider: str, model: str | None, base_url: str, api_key: str | None = None) -> None:
        self.provider = provider
        self.model = model or AUTO
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.note: str | None = None  # e.g. "llama-3.3-70b-versatile was retired — used openai/gpt-oss-120b"

    async def _resolve(self, c: httpx.AsyncClient, retired: str | None = None) -> str:
        key = f"{self.provider}|{self.base_url}"
        if key in _picked and time.monotonic() - _picked[key][0] < 86400 and _picked[key][1] != retired:
            return _picked[key][1]
        r = await c.get(f"{self.base_url}/models", headers=self.headers)
        r.raise_for_status()
        ids = [str(m.get("id")) for m in (r.json().get("data") or []) if m.get("id") and m.get("active", True) is not False]
        chosen = pick_model([i for i in ids if i != retired])
        if not chosen:
            raise RuntimeError(f"{self.provider}: no chat model available for this key (models: {', '.join(ids[:8]) or 'none'})")
        _picked[key] = (time.monotonic(), chosen)
        return chosen

    async def _chat(self, system: str, user: str, max_tokens: int, json_mode: bool) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=120) as c:
            if self.model == AUTO:
                self.model = await self._resolve(c)
            r = await self._post(c, system, user, max_tokens, json_mode)
            if model_missing(r):  # the configured / remembered model was retired: ask the provider what it has now
                old = self.model
                self.model = await self._resolve(c, retired=old)
                self.note = f"{old} is no longer available — used {self.model} (set it in Settings → AI providers)"
                r = await self._post(c, system, user, max_tokens, json_mode)
            r.raise_for_status()
            return r.json()

    async def _post(self, c: httpx.AsyncClient, system: str, user: str, max_tokens: int, json_mode: bool) -> httpx.Response:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return await c.post(f"{self.base_url}/chat/completions", json=body, headers=self.headers)

    async def review(self, packet: dict[str, Any]) -> Verdict:
        t0 = time.perf_counter()
        schema_hint = f"\n\nRespond with a single JSON object matching this JSON Schema:\n{VERDICT_SCHEMA}"
        data = await self._chat(system_for(packet) + schema_hint, packet_text(packet), 2000, json_mode=True)
        v = parse_verdict(self.provider, data.get("model", self.model), data["choices"][0]["message"]["content"])
        v.latency_ms = int((time.perf_counter() - t0) * 1000)
        return v

    async def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        data = await self._chat(system, user, max_tokens, json_mode=False)
        return data["choices"][0]["message"]["content"]
