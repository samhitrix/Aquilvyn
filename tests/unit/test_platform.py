import asyncio

import fakeredis.aioredis
import pytest

from fm_common.cache.l1 import MISSING, L1Cache
from fm_common.identity.rbac import has_permission
from fm_common.identity.tokens import create_access_token, create_service_token, decode_access_token
from fm_common.logging.masking import mask_pii_processor, scrub_text
from fm_common.ratelimit.engine import SlidingWindowLimiter


def test_pii_masking():
    s = scrub_text("pan ABCDE1234F aadhaar 1234 5678 9012 phone +91 9876543210 mail rahul.sharma@gmail.com card 4111 1111 1111 1111")
    assert "ABCDE1234F" not in s and "[PAN]" in s and "[AADHAAR]" in s and "[PHONE]" in s and "r***@gmail.com" in s and "****1111" in s
    ev = mask_pii_processor(None, "info", {"event": "x", "password": "hunter2", "user_id": "u1", "nested": {"token": "abc"}})
    assert ev["password"] == "[REDACTED]" and ev["nested"]["token"] == "[REDACTED]" and ev["user_id"] == "u1"


def test_l1_lru_and_ttl():
    c = L1Cache(max_items=2, default_ttl=10)
    c.set("a", 1); c.set("b", 2); c.get("a"); c.set("c", 3)
    assert c.get("b") is MISSING and c.get("a") == 1
    c.set("x", 1, ttl=-1)
    assert c.get("x") is MISSING


def test_sliding_window_blocks_after_limit():
    async def go():
        lim = SlidingWindowLimiter(fakeredis.aioredis.FakeRedis())
        results = [await lim.hit("ip:1", limit=3, window_seconds=60) for _ in range(5)]
        return results
    r = asyncio.run(go())
    assert [x.allowed for x in r] == [True, True, True, False, False]
    assert r[2].remaining == 0 and r[-1].reset_after_ms > 0


def test_jwt_roundtrip_and_service_tokens():
    import uuid

    uid, hid = uuid.uuid4(), uuid.uuid4()
    tok, ttl = create_access_token(uid, hid, "member")
    c = decode_access_token(tok)
    assert c.sub == uid and c.household_id == hid and ttl == 900
    svc = decode_access_token(create_service_token("advisor", hid))
    assert svc.typ == "service" and svc.role == "service"


def test_rbac():
    assert has_permission("viewer", "profile:read") and not has_permission("viewer", "transaction:write")
    assert has_permission("owner", "household:manage") and not has_permission("admin", "household:manage")
    assert not has_permission("advisor", "transaction:write")


def test_crypto_roundtrip():
    from fm_common.crypto import decrypt, encrypt

    ct = encrypt("ABCDE1234F", aad="profile-1")
    assert ct.startswith("v1:") and decrypt(ct, aad="profile-1") == "ABCDE1234F"
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        decrypt(ct, aad="profile-2")  # bound to its record


def test_database_url_built_from_parts_and_escaped():
    from fm_common.config import CommonSettings, build_database_url

    url = build_database_url("fm", "p@ss:w/rd#1", "pgbouncer", 6432, "foliosense")
    assert url == "postgresql+asyncpg://fm:p%40ss%3Aw%2Frd%231@pgbouncer:6432/foliosense"
    s = CommonSettings(postgres_password="secret", db_host="db", db_port=5433, database_url="")
    assert s.database_url.endswith(":secret@db:5433/foliosense")
    assert CommonSettings(database_url="postgresql+asyncpg://x:y@z/w").database_url == "postgresql+asyncpg://x:y@z/w"  # override wins


def test_ai_provider_settings_ignore_pasted_whitespace():
    from advisor_svc.ai.registry import make_client

    c = make_client("gemini", "\tgemini-3-flash-preview \n", " key\t", None)
    assert c is not None and c.model == "gemini-3-flash-preview" and c.api_key == "key"  # '\t' in the URL broke every call
    assert make_client("gemini", " \t", "key", None) is None


def test_groq_and_cloudflare_clients():
    from advisor_svc.ai.registry import make_client

    g = make_client("groq", "llama-3.3-70b-versatile", "gsk_x", None)
    assert g.provider == "groq" and g.base_url == "https://api.groq.com/openai/v1"
    cf = make_client("cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "tok", " acc123 ")
    assert cf.base_url == "https://api.cloudflare.com/client/v4/accounts/acc123/ai/v1"
    assert make_client("cloudflare", "@cf/x", "tok", None) is None  # needs the account ID


def test_quota_errors_pause_a_provider_and_the_next_one_answers(monkeypatch):
    import asyncio

    import httpx

    from advisor_svc.ai import registry

    store: dict[str, bytes] = {}

    class FakeRedis:
        async def set(self, k, v, ex=None):
            store[k] = v

        async def get(self, k):
            return store.get(k)

        async def delete(self, k):
            store.pop(k, None)

    import fm_common.redis as fr
    monkeypatch.setattr(fr, "get_redis", lambda: FakeRedis())

    class Quota:
        provider, model = "gemini", "gemini-3-flash"

        async def complete(self, *a, **k):
            req = httpx.Request("POST", "https://x")
            raise httpx.HTTPStatusError("429", request=req, response=httpx.Response(429, request=req, json={"error": {"message": "Quota exceeded"}}))

    class Works:
        provider, model = "groq", "llama"

        async def complete(self, *a, **k):
            return "OK"

    text, used, errors = asyncio.run(registry.complete_with_fallback("hh", [Quota(), Works()], "s", "u"))
    assert text == "OK" and used.provider == "groq" and errors[0]["status"] == 429
    paused = asyncio.run(registry.paused("hh"))
    assert "gemini" in paused and "Quota exceeded" in paused["gemini"]["reason"]   # skipped for a day
    assert registry.is_quota_error({"status": 403, "error": "RESOURCE_EXHAUSTED"}) and not registry.is_quota_error({"status": 401, "error": "bad key"})
    asyncio.run(registry.unpause("hh", "gemini"))
    assert asyncio.run(registry.paused("hh")) == {}


def test_internal_service_calls_are_never_rate_limited_as_one_user():
    """Every service token has the same system user: keyed together, the readiness engine's calls throttled the advisor."""
    import uuid

    from fm_common.identity.tokens import create_access_token, create_service_token, peek_subject

    assert peek_subject(create_service_token("readiness", uuid.uuid4())) == "service"
    uid = uuid.uuid4()
    assert peek_subject(create_access_token(uid, uuid.uuid4(), "owner")[0]) == str(uid)
    assert peek_subject("not-a-token") is None


def test_profile_email_and_mobile_are_optional_and_tidied():
    import pytest

    from portfolio_svc.api import ProfilePatch

    assert ProfilePatch(email="", mobile="  ").model_dump(exclude_unset=True) == {"email": None, "mobile": None}  # blank = not set
    p = ProfilePatch(email=" Ravi.K@Example.com ", mobile="98765 43210")
    assert (p.email, p.mobile) == ("ravi.k@example.com", "+919876543210")
    assert ProfilePatch(mobile="+44 20 7946 0958").mobile == "+442079460958"
    for bad in ({"email": "not-an-email"}, {"mobile": "12345"}):
        with pytest.raises(ValueError):
            ProfilePatch(**bad)


def test_gmail_login_needs_oidc_enabled_and_both_keys():
    from identity_svc.config import IdentitySettings

    keys = {"oidc_client_id": "id.apps.googleusercontent.com", "oidc_client_secret": "secret"}
    assert IdentitySettings(oidc_enabled=True, **keys).sso_ready
    assert not IdentitySettings(oidc_enabled=False, **keys).sso_ready  # keys alone don't show the button
    assert not IdentitySettings(oidc_enabled=True, oidc_client_id="id", oidc_client_secret=" ").sso_ready
    assert not IdentitySettings(oidc_enabled=True).sso_ready
