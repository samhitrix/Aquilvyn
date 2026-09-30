"""Mask secrets in text that gets stored, logged or shown — provider errors often echo the request URL,
and some APIs (Finnhub, Alpha Vantage, Gemini) take the key as a query parameter."""
from __future__ import annotations

import re

_QUERY = re.compile(r"(?i)\b(token|apikey|api_key|api-key|key|access_token|secret|password|crumb)=([^&\s'\"<>]+)")
_BEARER = re.compile(r"(?i)\b(bearer|authorization:)\s+[A-Za-z0-9._\-~+/=]{8,}")
_KEYLIKE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{12,}|gsk_[A-Za-z0-9]{12,}|AIza[0-9A-Za-z_\-]{20,})")


def _mask(v: str) -> str:
    return "***" if len(v) <= 8 else f"{v[:2]}***{v[-2:]}"


def redact(text: object) -> str:
    """'...?symbol=BEL.NS&token=abc123def456' → '...?symbol=BEL.NS&token=ab***56'."""
    s = str(text)
    s = _QUERY.sub(lambda m: f"{m.group(1)}={_mask(m.group(2))}", s)
    s = _BEARER.sub(lambda m: f"{m.group(1)} ***", s)
    return _KEYLIKE.sub(lambda m: _mask(m.group(1)), s)
