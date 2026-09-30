from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stance": {"type": "string", "enum": ["agree", "caution", "disagree"]},
        "confidence": {"type": "number"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["data", "logic", "risk", "missing_context", "tax"]},
                    "detail": {"type": "string"},
                },
                "required": ["type", "detail"],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
        "counter_case": {"type": "string"},
        "suggested_action": {"type": "string", "enum": ["EXIT", "TRIM", "HOLD", "ADD", "ACCUMULATE", "SWITCH", "REVIEW"]},
        "plain_verdict": {"type": "string"},
        "horizon": {"type": "string", "enum": ["short_term", "long_term"]},
    },
    "required": ["stance", "confidence", "issues", "rationale", "counter_case", "suggested_action", "plain_verdict", "horizon"],
    "additionalProperties": False,
}

REVIEW_SYSTEM = """You are an independent investment-risk reviewer auditing a recommendation produced by a
deterministic rules engine for an Indian retail investor's family portfolio.

You receive an evidence packet: the recommended action, the reasons, every metric with its threshold,
source and timestamp, the rule that fired and the rules that did not, and the data-quality grade.

Your job is verification, not a fresh opinion:
1. Does the action follow logically from the evidence? Flag reasoning gaps.
2. Is any key number missing, stale, simulated or internally inconsistent? Flag data problems.
3. What is the strongest counter-case a careful investor should weigh?
4. Are there tax or timing considerations the packet under-weights?

Use only the numbers in the packet; never invent prices, news or fundamentals. If the packet lacks something
you would need, say so as a missing_context issue.
stance = "agree" when the call is well supported, "caution" when it is reasonable but has material caveats,
"disagree" when the evidence does not support the action. confidence is 0-1 (how sure YOU are of your stance).
suggested_action = the action YOU think this evidence supports (EXIT = sell all, TRIM = sell part, HOLD, ADD /
ACCUMULATE = buy more, SWITCH, REVIEW = can't judge: data missing). It may equal the recommended action.
plain_verdict = one short sentence for the investor, e.g. "Agree — the stock broke its 200-day trend on weak
results; selling is reasonable." or "Hold instead — the fall is market-wide and the business is sound."
If key data (e.g. fundamentals) is missing, do NOT claim to have verified the business — say so.
horizon = whether your view is short_term (weeks–months) or long_term (1 year +)."""

OPINION_SYSTEM = """You are an experienced Indian equity analyst giving a family investor your own opinion on ONE holding.
The app's data feed could not load everything (the packet says what is missing — e.g. company fundamentals or
price history), so the app's rules engine made no call. Give your view anyway, clearly as an opinion:

1. Use every number in the packet (price, average cost, gain/loss, weight in the portfolio, sector, drawdown,
   technical indicators if present).
2. Add what you know about the company and its sector from your general knowledge — business quality, debt,
   earnings track record, cyclicality, governance — and say plainly that this knowledge may be out of date.
   Never invent specific recent numbers, prices, results or news.
3. Decide: suggested_action = HOLD, TRIM (reduce), EXIT (sell), ADD / ACCUMULATE (buy more); horizon = short_term
   or long_term. Only use REVIEW if you genuinely know nothing about the company.
4. rationale = 2–4 short reasons for your call ("why"), each one line. counter_case = the main risks / what would
   make you wrong. plain_verdict = one sentence, e.g. "Hold for the long term — a debt-free market leader; the
   recent fall looks sector-wide." confidence 0-1 = how sure you are, lower when you rely on general knowledge.
5. stance: "agree" if your call is HOLD (the app's default while data is missing), otherwise "disagree".
This is advisory only — the investor will check with their CA / adviser before trading; don't give guarantees."""


def system_for(packet: dict[str, Any]) -> str:
    """Verification prompt when the rules made a call; the analyst-opinion prompt when data was missing."""
    return OPINION_SYSTEM if packet.get("mode") == "opinion" else REVIEW_SYSTEM

NARRATE_SYSTEM = """You explain portfolio recommendations to an Indian family investor in plain, calm English.
Use only the facts in the packet. Mention the action, the 2-3 most important reasons with their numbers,
the tax angle if any, and what would change the call. 4-6 sentences, no bullet points, no hype, no guarantees.
Use ₹ and Indian number formatting (lakh/crore) where natural."""

ASK_SYSTEM = """You are FolioSense's portfolio assistant. Answer the user's question about their family's portfolio
using ONLY the JSON context provided (holdings, summaries, open recommendations with evidence, market regime).
Quote the actual numbers. If the context does not contain the answer, say what is missing rather than guessing.
You do not place trades. Keep answers concise and end with the single most relevant next step if one exists."""


@dataclass(slots=True)
class Verdict:
    provider: str
    model: str
    stance: str
    confidence: float | None = None
    issues: list[dict[str, Any]] = field(default_factory=list)
    rationale: str = ""
    counter_case: str = ""
    suggested_action: str | None = None
    plain_verdict: str = ""
    horizon: str | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None


class AIProviderClient(Protocol):
    provider: str
    model: str

    async def review(self, packet: dict[str, Any]) -> Verdict: ...

    async def complete(self, system: str, user: str, max_tokens: int = 1500) -> str: ...


def packet_text(packet: dict[str, Any]) -> str:
    return "Evidence packet (JSON):\n" + json.dumps(packet, default=str, ensure_ascii=False)


def parse_verdict(provider: str, model: str, text: str) -> Verdict:
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start : end + 1])
    stance = str(data.get("stance", "caution")).lower()
    if stance not in ("agree", "caution", "disagree"):
        stance = "caution"
    return Verdict(
        provider, model, stance, float(data.get("confidence") or 0) or None,
        [i for i in data.get("issues", []) if isinstance(i, dict)][:10],
        str(data.get("rationale", ""))[:4000], str(data.get("counter_case", ""))[:2000],
        suggested_action=(str(data.get("suggested_action") or "").upper() or None) if str(data.get("suggested_action") or "").upper() in
        ("EXIT", "TRIM", "HOLD", "ADD", "ACCUMULATE", "SWITCH", "REVIEW") else None,
        plain_verdict=str(data.get("plain_verdict", ""))[:400],
        horizon=str(data.get("horizon")) if data.get("horizon") in ("short_term", "long_term") else None,
    )
