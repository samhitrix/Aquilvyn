"""OpenAI-compatible chat-completions adapter — used for OpenAI and for local Ollama
(which serves the same ``/v1/chat/completions`` API)."""
from __future__ import annotations

import time
from typing import Any

import httpx

from .base import VERDICT_SCHEMA, Verdict, packet_text, parse_verdict, system_for


class OpenAICompatClient:
    def __init__(self, provider: str, model: str, base_url: str, api_key: str | None = None) -> None:
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def _chat(self, system: str, user: str, max_tokens: int, json_mode: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{self.base_url}/chat/completions", json=body, headers=self.headers)
            r.raise_for_status()
            return r.json()

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
