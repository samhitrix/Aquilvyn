"""PII / sensitive-data masking processor for structlog."""
from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "password", "passwd", "secret", "token", "access_token", "refresh_token", "id_token",
        "authorization", "cookie", "set-cookie", "api_key", "apikey", "client_secret",
        "otp", "pin", "cvv", "private_key", "jwt_secret", "s3_secret_key",
    }
)

# Order matters: more specific patterns first.
_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    # Bearer tokens / JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"), "[JWT]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*"), "Bearer [REDACTED]"),
    # Email: keep first char + domain for debuggability
    (
        re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"),
        lambda m: f"{m.group(1)}***@{m.group(2)}",
    ),
    # Indian PAN: ABCDE1234F
    (re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), "[PAN]"),
    # Payment card (13-19 digits, optional separators) — before Aadhaar/phone
    (re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), lambda m: _mask_digits(m.group(0))),
    # Aadhaar: 12 digits in 4-4-4 groups
    (re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b"), "[AADHAAR]"),
    # Phone numbers (+91 98xxxxxxx etc.)
    (re.compile(r"(?<!\d)(?:\+?\d{1,3}[ -]?)?[6-9]\d{9}(?!\d)"), "[PHONE]"),
]


def _mask_digits(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 12:  # most likely Aadhaar, not a card
        return "[AADHAAR]"
    return f"[CARD ****{digits[-4:]}]"


def scrub_text(value: str) -> str:
    for pattern, repl in _PATTERNS:
        value = pattern.sub(repl, value)
    return value


def _mask(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if str(k).lower() in SENSITIVE_KEYS else _mask(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_mask(v, depth + 1) for v in value)
    return value


# Keys the engine itself injects; they are trusted and must stay readable.
_PASSTHROUGH = frozenset({"timestamp", "level", "logger", "service", "trace_id", "span_id", "user_id", "household_id", "request_id", "client_ip"})


def mask_pii_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict.keys()):
        if key in _PASSTHROUGH:
            continue
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = "[REDACTED]"
        else:
            event_dict[key] = _mask(event_dict[key])
    return event_dict
