"""Field-level encryption (AES-256-GCM) for PAN numbers and AI provider API keys.

Key: ``FM_ENCRYPTION_KEY`` (urlsafe-base64, 32 bytes). Ciphertext format: ``v1:<b64(nonce|ct)>``
so the key can be rotated later (v2 …) without a flag day."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from fm_common.config import settings

# dev-only fallback key; its bytes predate the rename and must not change, or dev data (PANs, AI keys) stops decrypting
_DEV_KEY = hashlib.sha256(b"foliomatrix-dev-only-key").digest()


def _key() -> bytes:
    raw = os.environ.get("FM_ENCRYPTION_KEY")
    if raw:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(key) != 32:
            raise ValueError("FM_ENCRYPTION_KEY must decode to 32 bytes")
        return key
    if settings.environment in ("prod", "staging"):
        raise RuntimeError("FM_ENCRYPTION_KEY is required outside local/dev")
    return _DEV_KEY


def encrypt(plaintext: str, aad: str = "") -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, plaintext.encode(), aad.encode() or None)
    return "v1:" + base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt(token: str, aad: str = "") -> str:
    if not token.startswith("v1:"):
        raise ValueError("unknown ciphertext version")
    blob = base64.urlsafe_b64decode(token[3:])
    return AESGCM(_key()).decrypt(blob[:12], blob[12:], aad.encode() or None).decode()


def mask_tail(value: str, keep: int = 4) -> str:
    return "•" * max(0, len(value) - keep) + value[-keep:]


PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def _fingerprint(kind: str, value: str) -> str:
    # separate derived key so fingerprints can't be used to test encryption keys
    key = hashlib.sha256(b"fm-fingerprint-v1|" + _key()).digest()
    return hmac.new(key, f"{kind}|{value}".encode(), hashlib.sha256).hexdigest()


def normalise_pan(pan: str) -> str:
    return re.sub(r"\s+", "", pan or "").upper()


def pan_fingerprint(pan: str) -> str:
    """Keyed hash of a PAN: lets profiles be *found* by PAN (e.g. from a CAS) without storing it
    in clear. Case- and whitespace-insensitive."""
    return _fingerprint("pan", normalise_pan(pan))


def account_fingerprint(kind: str, account_id: str) -> str:
    """Same idea for broker client IDs (kind = 'zerodha', …)."""
    return _fingerprint(f"acct:{kind.lower()}", re.sub(r"\s+", "", account_id or "").upper())


def mask_pan(pan: str) -> str:
    p = normalise_pan(pan)
    return f"XXXXXX{p[-4:]}" if len(p) == 10 else mask_tail(p)
