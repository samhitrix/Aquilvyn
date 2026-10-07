"""Google Gemini adapter (REST ``generateContent`` with JSON response mode)."""
from __future__ import annotations

import time
from typing import Any

import httpx

from .base import VERDICT_SCHEMA, Verdict, packet_text, parse_verdict, system_for

BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiClient:
    provider = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    async def _generate(self, system: str, user: str, max_tokens: int, json_mode: bool) -> str:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, **({"responseMimeType": "application/json"} if json_mode else {})},
        }
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{BASE}/{self.model}:generateContent", json=body, headers={"x-goog-api-key": self.api_key})
            r.raise_for_status()
            cands = r.json().get("candidates") or [{}]
            parts = (cands[0].get("content") or {}).get("parts") or []  # thinking models may return no text part
            return "".join(p.get("text", "") for p in parts)

    async def review(self, packet: dict[str, Any]) -> Verdict:
        t0 = time.perf_counter()
        text = await self._generate(system_for(packet) + f"\n\nReturn JSON matching: {VERDICT_SCHEMA}", packet_text(packet), 4000, True)
        v = parse_verdict(self.provider, self.model, text)
        v.latency_ms = int((time.perf_counter() - t0) * 1000)
        return v

    async def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        return await self._generate(system, user, max_tokens, False)
