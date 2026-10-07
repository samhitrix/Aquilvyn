"""Readiness engine: the four questions asked about every holding, and what it does when the answer is no."""
from datetime import UTC, datetime, timedelta

from readiness_svc import checks as ck

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
TODAY = NOW.date()


def cov(bars=300, last=TODAY - timedelta(days=1), fields=("pe", "pb", "roe", "market_cap", "debt_to_equity"), fund_age_days=1):
    return {"bars_400d": bars, "last_bar": last.isoformat() if last else None,
            "fundamentals": {"as_of": (NOW - timedelta(days=fund_age_days)).isoformat(), "source": "yahoo:yahoo-api", "fields": list(fields)}
            if fields is not None else None}


def holding(asset_type="stock", fund_available=True, bars=560, reviewed=True, last_error=None, consensus="verified"):
    return {"rec_id": "r1", "profile_id": "p1", "instrument_id": "i1", "symbol": "BEL.NS", "asset_type": asset_type,
            "built_at": (NOW - timedelta(hours=2)).isoformat(),
            "report": {"bars": bars, "fundamentals_available": fund_available},
            "ai": {"reviewed": reviewed, "consensus": consensus, "last_error": last_error}}


def states(h, c, providers=("cloudflare",), paused=None):
    return {k: v.state for k, v in ck.evaluate(h, c, list(providers), paused or {}, NOW).items()}


def test_a_healthy_stock_is_ready():
    assert states(holding(), cov()) == {"fundamentals": "ok", "technicals": "ok", "analysis": "ok", "ai_review": "ok"}


def test_missing_or_thin_or_old_fundamentals_fail():
    assert ck.check_fundamentals("stock", cov(fields=None), NOW).state == "failed"
    thin = ck.check_fundamentals("stock", cov(fields=("pe", "pb")), NOW)
    assert thin.state == "failed" and "only 2 key figures" in thin.detail
    old = ck.check_fundamentals("stock", cov(fund_age_days=12), NOW)
    assert old.state == "failed" and "12 days ago" in old.detail
    assert ck.check_fundamentals("mutual_fund", None, NOW).state == "n/a"  # funds have no company fundamentals


def test_short_or_old_price_history_fails_technicals():
    assert ck.check_technicals("stock", cov(bars=40), TODAY).detail.startswith("only 40 days")
    assert "9 days old" in ck.check_technicals("stock", cov(last=TODAY - timedelta(days=9)), TODAY).detail
    assert ck.check_technicals("mutual_fund", cov(last=TODAY - timedelta(days=5)), TODAY).state == "ok"  # NAVs lag a little
    assert ck.check_technicals("epf", None, TODAY).state == "n/a"
    assert ck.check_technicals("stock", None, TODAY).state == "failed"


def test_a_call_built_before_the_data_loaded_is_stale():
    """The BEL case: the sources return fundamentals now, but the report still says 'no fundamentals'."""
    s = states(holding(fund_available=False, reviewed=True), cov())
    assert s["analysis"] == "failed" and s["ai_review"] == "waiting"  # re-analyse first, then the AI reviews the new call
    assert states(holding(bars=30), cov())["analysis"] == "failed"
    # data still missing → nothing to re-analyse yet; wait for the data fix
    assert states(holding(fund_available=False), cov(fields=None))["analysis"] == "waiting"


def test_ai_review_states():
    assert states(holding(reviewed=False), cov())["ai_review"] == "failed"
    c = ck.check_ai(holding(reviewed=False, last_error="groq: HTTP 429"), ["groq"], {}, ck.Check("ok", ""))
    assert c.state == "failed" and "429" in c.detail
    assert ck.check_ai(holding(reviewed=False), [], {}, ck.Check("ok", "")).state == "n/a"  # no AI connected is not a failure
    allp = ck.check_ai(holding(reviewed=False), ["gemini"], {"gemini": "quota"}, ck.Check("ok", ""))
    assert allp.state == "failed" and "paused" in allp.detail


def test_fixes_back_off_and_stop_being_quiet():
    backoff = (600, 1800, 7200, 43200)
    f = None
    waits = []
    for _ in range(5):
        f = ck.record_attempt(f, NOW, backoff, "refresh_fundamentals", "HTTP 403")
        waits.append((datetime.fromisoformat(f["next_try"]) - NOW).total_seconds())
    assert waits == [600, 1800, 7200, 43200, 43200] and f["attempts"] == 5 and f["last_error"] == "HTTP 403"
    checks = ck.evaluate(holding(), cov(fields=None), ["cloudflare"], {}, NOW)
    assert ck.plan_fixes(checks, {}, NOW) == ["fundamentals"]
    assert ck.plan_fixes(checks, {"fundamentals": f}, NOW) == []  # not due yet
    assert ck.status_of(checks, {"fundamentals": f}, 4) == "attention"
    assert ck.status_of(checks, {"fundamentals": {"attempts": 1}}, 4) == "fixing"
    assert ck.status_of(ck.evaluate(holding(), cov(), ["x"], {}, NOW), {}, 4) == "ready"


def test_secrets_are_masked_in_errors():
    from fm_common.redact import redact

    msg = "403 Forbidden for url 'https://finnhub.io/api/v1/stock/metric?symbol=BEL.NS&metric=all&token=dats1s1r01qkvn4lumc0'"
    out = redact(msg)
    assert "dats1s1r01qkvn4lumc0" not in out and "symbol=BEL.NS" in out and "token=da***c0" in out
    assert "gsk_abcdefghijklmnop" not in redact("key gsk_abcdefghijklmnop rejected")
    assert "secret123456" not in redact("Authorization: Bearer secret123456")


def test_env_ai_providers_never_jump_ahead_of_saved_ones(monkeypatch):
    """Gemini in .env with AI_PRIMARY_PROVIDER=gemini used to be tried before the providers saved in Settings."""
    import asyncio
    import uuid
    from types import SimpleNamespace

    from advisor_svc.ai import registry

    monkeypatch.setattr(registry.settings, "ai_primary_provider", "gemini")
    monkeypatch.setattr(registry, "_env_defaults", lambda: [
        {"provider": "gemini", "model": "gemini-3-flash", "api_key": "k", "base_url": None},
        {"provider": "cloudflare", "model": "@cf/x", "api_key": "k", "base_url": "acct"},
    ])

    async def no_pauses(hid):
        return {}
    monkeypatch.setattr(registry, "paused", no_pauses)
    saved = SimpleNamespace(provider="cloudflare", model="@cf/x", is_enabled=True, api_key_encrypted=None, base_url="acct", priority=1)

    class DB:
        def __init__(self, rows):
            self.rows = rows

        async def execute(self, _):
            return SimpleNamespace(scalars=lambda: self.rows)

    chain, _ = asyncio.run(registry.household_clients(DB([saved]), uuid.uuid4()))
    assert [c.provider for c in chain] == ["cloudflare", "gemini"]
    chain, _ = asyncio.run(registry.household_clients(DB([]), uuid.uuid4()))
    assert [c.provider for c in chain] == ["gemini", "cloudflare"]  # nothing saved: .env's primary goes first
    off = SimpleNamespace(provider="gemini", model="gemini-3-flash", is_enabled=False, api_key_encrypted=None, base_url=None, priority=2)
    chain, _ = asyncio.run(registry.household_clients(DB([saved, off]), uuid.uuid4()))
    assert [c.provider for c in chain] == ["cloudflare"]  # switched off in Settings → never used


def test_an_earlier_ai_review_stays_valid_until_the_new_one_lands():
    h = holding(reviewed=False)
    h["ai"]["earlier_review_at"] = "2026-09-29T11:40:00+00:00"
    c = ck.check_ai(h, ["cloudflare"], {}, ck.Check("ok", ""))
    assert c.state == "ok" and "2026-09-29" in c.detail and "re-check" in c.detail


def test_gold_bonds_and_g_secs_need_no_company_fundamentals():
    from datetime import UTC, datetime

    from readiness_svc.checks import check_fundamentals

    now = datetime.now(UTC)
    assert check_fundamentals("stock", None, now, "SGBSEP31II-GB.NS").state == "n/a"
    assert check_fundamentals("stock", None, now, "GS2033-GS.NS").state == "n/a"
    assert check_fundamentals("stock", None, now, "TCS.NS").state == "failed"
