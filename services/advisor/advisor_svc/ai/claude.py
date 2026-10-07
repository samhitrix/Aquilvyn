"""Claude adapter (official Anthropic SDK, async). Structured JSON output via
``output_config.format``; adaptive thinking; server-side refusal fallback on models that support it."""
from __future__ import annotations

import time
from typing import Any

import anthropic

from .base import VERDICT_SCHEMA, Verdict, packet_text, parse_verdict, system_for

# $ per 1M tokens (input, output) — used for the per-household AI budget.
PRICES = {"claude-opus-5": (5.0, 25.0), "claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5": (2.0, 10.0),
          "claude-haiku-4-5": (1.0, 5.0), "claude-fable-5-1": (10.0, 50.0)}
FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5", "claude-fable-5-1"}


class ClaudeClient:
    provider = "claude"

    def __init__(self, api_key: str, model: str = "claude-opus-5") -> None:
        self.model = model
        self.client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2, timeout=120)

    async def _create(self, system: str, user: str, max_tokens: int, schema: dict[str, Any] | None) -> anthropic.types.beta.BetaMessage:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "medium", **({"format": {"type": "json_schema", "schema": schema}} if schema else {})},
        }
        if self.model in FALLBACK_MODELS:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        return await self.client.beta.messages.create(**kwargs)

    def _cost(self, msg: Any) -> float | None:
        p = PRICES.get(self.model)
        if not p or not getattr(msg, "usage", None):
            return None
        return round(msg.usage.input_tokens / 1e6 * p[0] + msg.usage.output_tokens / 1e6 * p[1], 5)

    @staticmethod
    def _text(msg: Any) -> str:
        if msg.stop_reason == "refusal":
            raise RuntimeError("Claude declined to review this packet")
        return "".join(b.text for b in msg.content if b.type == "text")

    async def review(self, packet: dict[str, Any]) -> Verdict:
        t0 = time.perf_counter()
        msg = await self._create(system_for(packet), packet_text(packet), 16000, VERDICT_SCHEMA)
        v = parse_verdict(self.provider, msg.model, self._text(msg))
        v.latency_ms = int((time.perf_counter() - t0) * 1000)
        v.cost_usd = self._cost(msg)
        return v

    async def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        return self._text(await self._create(system, user, max_tokens, None))
