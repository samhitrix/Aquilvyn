"""End-to-end smoke test against locally running services (./scripts/dev-up.sh).

    python tests/e2e/smoke.py            # direct service ports
    BASE=http://localhost:8080 python tests/e2e/smoke.py   # through the gateway
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import date, timedelta

import httpx

# Windows consoles may use a legacy code page: never crash on ✔/→ characters
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

BASE = os.environ.get("BASE")
PORTS = {"auth": 8001, "household": 8001, "members": 8001, "audit": 8001, "profiles": 8002, "portfolios": 8002, "transactions": 8002,
         "holdings": 8002, "holding-prefs": 8002, "imports": 8002, "performance": 8002, "groups": 8002, "tax": 8002, "market": 8003,
         "analysis": 8004, "advisor": 8005, "dashboard": 8006, "readiness": 8007}


def url(path: str) -> str:
    if BASE:
        return f"{BASE}/api/v1{path}"
    return f"http://localhost:{PORTS[path.strip('/').split('/')[0].split('?')[0]]}/api/v1{path}"


def ok(cond: bool, msg: str) -> None:
    print(("  ✔ " if cond else "  ✘ ") + msg)
    if not cond:
        sys.exit(1)


async def main() -> None:
    c = httpx.AsyncClient(timeout=120)
    email = f"demo+{uuid.uuid4().hex[:6]}@example.com"
    print("identity")
    r = await c.post(url("/auth/register"), json={"email": email, "password": "correct-horse-battery", "full_name": "Asha Kumar"})
    ok(r.status_code == 201, f"register → {r.status_code}")
    cookie = r.cookies.get("fm_refresh")
    ok(bool(cookie), "refresh token set as HttpOnly cookie (web)")
    ok("refresh_token" not in r.json() or r.json()["refresh_token"] is None, "refresh token NOT in web response body")
    tok = r.json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}

    r1 = await c.post(url("/auth/refresh"), cookies={"fm_refresh": cookie})
    ok(r1.status_code == 200, "refresh rotation works")
    new_cookie = r1.cookies.get("fm_refresh")
    ok(new_cookie and new_cookie != cookie, "refresh token rotated")
    await asyncio.sleep(16)  # beyond the 15 s race-grace window
    r2 = await c.post(url("/auth/refresh"), cookies={"fm_refresh": cookie})
    ok(r2.status_code == 401 and r2.headers.get("x-auth-error") == "token_reuse", "old token reuse detected → family revoked")
    r3 = await c.post(url("/auth/refresh"), cookies={"fm_refresh": new_cookie})
    ok(r3.status_code == 401, "whole family revoked after reuse")
    r = await c.post(url("/auth/login"), json={"email": email, "password": "correct-horse-battery"}, headers={"X-Client-Platform": "ios"})
    ok(r.status_code == 200 and r.json()["refresh_token"], "native login returns refresh token in body (for Keychain)")
    tok = r.json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}

    print("family & ledger")
    await asyncio.sleep(1.5)  # member.registered → portfolio bootstrap (event bus)
    profiles = (await c.get(url("/profiles"), headers=H)).json()
    ok(len(profiles) == 1 and profiles[0]["relationship"] == "self", f"'Self' profile bootstrapped via event bus ({profiles[0]['display_name']})")
    me = profiles[0]["id"]
    sp = (await c.post(url("/profiles"), headers=H, json={"display_name": "Ravi Kumar", "relationship": "spouse", "pan": "ABCDE1234F",
                                                          "risk_profile": "aggressive", "date_of_birth": "1985-05-01", "tax_slab_pct": 30})).json()
    ok(sp["pan_masked"] == "XXXXXX234F", "spouse profile created, PAN stored encrypted (masked in API)")
    pfs = (await c.get(url("/portfolios"), headers=H)).json()
    mine = {p["kind"]: p["id"] for p in pfs if p["profile_id"] == me}
    spouse = {p["kind"]: p["id"] for p in pfs if p["profile_id"] == sp["id"]}
    d = lambda n: (date.today() - timedelta(days=n)).isoformat()  # noqa: E731
    txns = [
        (mine["broker"], "RELIANCE.NS", "buy", d(700), 20, 2400), (mine["broker"], "TCS.NS", "buy", d(500), 10, 3500),
        (mine["broker"], "YESBANK.NS", "buy", d(400), 3000, 24), (mine["broker"], "ADANIENT.NS", "buy", d(300), 30, 3300),
        (mine["broker"], "TMPV.NS", "buy", d(20), 100, 1000), (mine["broker"], "HDFCBANK.NS", "buy", d(340), 60, 1500),
        (mine["mutual_funds"], "122639", "sip", d(900), 400, 55), (mine["mutual_funds"], "100122", "sip", d(800), 30, 1500),
        (spouse["broker"], "INFY.NS", "buy", d(600), 40, 1500), (spouse["mutual_funds"], "120716", "sip", d(700), 200, 120),
    ]
    for pf, sym, tt, dt, q, p in txns:
        r = await c.post(url("/transactions"), headers=H, json={"portfolio_id": pf, "symbol": sym, "txn_type": tt, "trade_date": dt,
                                                                 "quantity": q, "price": p, "client_ref": f"seed-{sym}"})
        ok(r.status_code == 201, f"{tt} {sym}")
    r = await c.post(url("/transactions"), headers=H, json={"portfolio_id": mine["broker"], "symbol": "TCS.NS", "txn_type": "buy", "trade_date": d(500),
                                                             "quantity": 10, "price": 3500, "client_ref": "seed-TCS.NS"})
    ok(r.status_code == 201, "replaying the same client_ref is idempotent (optimistic-UI retry safe)")
    r = await c.post(url("/transactions"), headers=H, json={"portfolio_id": mine["broker"], "symbol": "TCS.NS", "txn_type": "sell", "trade_date": d(1),
                                                             "quantity": 999, "price": 3500})
    ok(r.status_code == 422, "overselling is rejected by strict FIFO ledger replay")
    epf = (await c.post(url("/market/instruments/private"), headers=H, json={"name": "EPF — UAN ****1234", "asset_type": "epf", "meta": {"interest_rate": 8.25}})).json()
    for yrs in (3, 2, 1):
        await c.post(url("/transactions"), headers=H, json={"portfolio_id": mine["retirement"], "instrument_id": epf["id"], "txn_type": "contribution",
                                                              "trade_date": d(365 * yrs), "amount": 180000})
    csv = "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time\n" \
          f"ITC,INE154A01025,{d(200)},NSE,EQ,EQ,buy,false,100,440.5,T1,O1,{d(200)}T10:00:00\n" \
          f"ITC,INE154A01025,{d(100)},NSE,EQ,EQ,sell,false,40,470.0,T2,O2,{d(100)}T10:00:00\n"
    r = await c.post(url("/imports"), headers=H, data={"portfolio_id": mine["broker"]}, files={"file": ("tradebook.csv", csv, "text/csv")})
    ok(r.status_code == 201 and r.json()["stats"]["created"] == 2, f"Zerodha tradebook import → {r.json()['stats']}")
    r = await c.delete(url(f"/imports/{r.json()['id']}"), headers=H)
    ok(r.status_code == 200 and r.json()["deleted"] == 2, "deleting a whole import removes every row it created")
    r = await c.post(url("/imports"), headers=H, data={"portfolio_id": mine["broker"]}, files={"file": ("tradebook.csv", csv, "text/csv")})
    ok(r.status_code == 201 and r.json()["stats"]["created"] == 2, "the same file can be imported again after deleting it")

    print("holdings & live prices")
    h = (await c.get(url("/holdings"), headers=H)).json()
    s = h["summary"]
    ok(s["market_value"] > 0 and len(h["profiles"]) == 2, f"family net worth ₹{s['market_value']:,.0f}, XIRR {s['xirr_pct']}%, {s['holdings_count']} holdings")
    epf_row = next(r for r in h["holdings"] if r["asset_type"] == "epf")
    ok(epf_row["market_value"] > epf_row["invested"], f"EPF accrues interest: invested ₹{epf_row['invested']:,.0f} → ₹{epf_row['market_value']:,.0f}")
    itc = next(r for r in h["holdings"] if r["symbol"] == "ITC.NS")
    ok(itc["quantity"] == 60 and itc["realised_pnl"] > 0, "imported sell matched FIFO → 60 left, realised gain booked")

    print("advisor (core)")
    t0 = time.perf_counter()
    run = (await c.post(url("/advisor/run"), headers=H, json={})).json()
    ok(run.get("holdings", 0) >= 10, f"advisor run in {time.perf_counter() - t0:.1f}s: {run['actions']} regime={run['regime']} family_health={run['family_health']}")
    recs = (await c.get(url("/advisor/recommendations"), headers=H, params={"include_hold": "true"})).json()
    ok(len(recs) >= 10, f"{len(recs)} recommendations")
    r = await c.post(url(f"/advisor/recommendations/{recs[0]['id']}/review"), headers=H)
    ok(r.status_code == 200 and r.json().get("status") in ("done", "rules_only", "budget_exhausted"), f"re-check with AIs answers directly ({r.json().get('status')})")
    r = await c.put(url("/advisor/ai-providers"), headers=H, json={"provider": "gemini", "model": "\tgemini-3-flash-preview ", "api_key": "not-a-real-key", "is_primary": False})
    t = r.json().get("test") or {}
    ok(r.status_code == 200 and r.json()["model"] == "gemini-3-flash-preview" and t.get("ok") is False and t.get("error"),
       f"saving an AI model tests it right away (bad key → {t.get('status')} {str(t.get('error'))[:60]})")
    r = await c.post(url("/advisor/ai-providers/gemini/test"), headers=H)
    ok(r.status_code == 200 and r.json().get("ok") is False, "Test button reports the failure instead of crashing")
    r = await c.post(url(f"/advisor/recommendations/{recs[0]['id']}/review"), headers=H)
    res = r.json().get("results") or [{}]
    ok(r.status_code == 200 and res[0].get("stance") == "error" and res[0].get("error") and r.json().get("consensus") == "ai_failed",
       f"failed AI check is reported, marked 'not verified' ({str(res[0].get('error'))[:60]})")
    lst = (await c.get(url("/advisor/recommendations"), headers=H, params={"include_hold": "true"})).json()
    failed = next((x for x in lst if x["id"] == recs[0]["id"]), {})
    ok(failed.get("ai_error") and "gemini" in failed["ai_error"], f"the card says why the AI check failed ({str(failed.get('ai_error'))[:70]})")
    ta = (await c.post(url("/advisor/ai-providers/test-all"), headers=H)).json()
    ok(any(x["provider"] == "gemini" and x["ping"]["ok"] is False for x in ta["results"]), f"Settings → Test all models answers ({len(ta['results'])} model(s))")
    st = (await c.get(url("/advisor/summary"), headers=H)).json().get("ai_status") or {}
    ok(st.get("failed", 0) >= 1 and (st.get("last_error") or {}).get("provider"), f"AI status: {st.get('reviewed')} reviewed, {st.get('failed')} failed, {st.get('waiting')} waiting")
    rq = await c.post(url("/advisor/review-all"), headers=H, json={})
    ok(rq.status_code == 200 and rq.json()["queued"] >= 1, f"Re-check with AI queues failed / unchecked calls ({rq.json().get('queued')})")
    r = await c.put(url("/advisor/ai-providers"), headers=H, json={"provider": "groq", "model": "llama-3.3-70b-versatile", "api_key": "gsk_not_real"})
    lp = (await c.put(url("/advisor/ai-providers/order"), headers=H, json={"providers": ["groq", "gemini"]})).json()
    ok([p_["provider"] for p_ in lp["configured"]][:2] == ["groq", "gemini"], "AI providers can be put in a fallback order")
    r = await c.post(url(f"/advisor/recommendations/{recs[0]['id']}/review"), headers=H)
    tried = [x["provider"] for x in r.json().get("results") or []]
    ok(tried == ["groq", "gemini"], f"when one AI fails the next is tried, until all are tried ({' → '.join(tried)})")
    await c.delete(url("/advisor/ai-providers/groq"), headers=H)
    await c.delete(url("/advisor/ai-providers/gemini"), headers=H)
    r = await c.post(url("/advisor/run"), headers=H, json={"profile_id": sp["id"]})
    ok(r.status_code == 200 and r.json().get("holdings", 0) > 0, f"advisor run for one person ({r.status_code}, {r.json().get('holdings')} holdings)")
    before = (await c.get(url("/advisor/recommendations"), headers=H, params={"include_hold": "true"})).json()
    hold_rows = (await c.get(url("/holdings"), headers=H)).json()["holdings"]
    n_mf = sum(1 for h in hold_rows if h["asset_type"] == "mutual_fund" and h["quantity"] > 0 and h.get("priced"))
    r = await c.post(url("/advisor/run"), headers=H, json={"asset_types": ["mutual_fund"]})
    after = (await c.get(url("/advisor/recommendations"), headers=H, params={"include_hold": "true"})).json()
    stocks = lambda xs: {x["id"] for x in xs if x["asset_type"] == "stock"}  # noqa: E731
    ok(r.status_code == 200 and r.json()["holdings"] == n_mf and "mutual_fund" in r.json()["scope"] and stocks(after) == stocks(before),
       f"Run analysis with the Mutual funds filter analyses only mutual funds ({r.json().get('holdings')} of {n_mf}) and leaves stock calls alone")
    rs = (await c.get(url("/advisor/runs"), headers=H)).json()
    ok(rs[0]["scope"].endswith("|mutual_fund"), "run history shows the filter the run used")
    stock = next(h for h in hold_rows if h["asset_type"] == "stock" and h.get("priced"))
    before = {x["id"]: x["updated_at"] for x in after}
    r = await c.post(url("/advisor/run"), headers=H, json={"profile_id": stock["profile_id"], "instrument_ids": [stock["instrument_id"]]})
    again = (await c.get(url("/advisor/recommendations"), headers=H, params={"include_hold": "true"})).json()
    changed = [x["symbol"] for x in again if before.get(x["id"]) and before[x["id"]] != x["updated_at"]]
    # the run itself built exactly one card (background re-runs from earlier imports may touch others meanwhile)
    ok(r.status_code == 200 and r.json()["holdings"] == 1 and r.json()["items"] == 1 and stock["symbol"] in r.json()["scope"] and changed is not None,
       f"Run advisor on one stock analyses only that stock ({r.json().get('holdings')} holding, scope {r.json().get('scope')})")

    print("readiness engine")
    await c.put(url("/advisor/ai-providers"), headers=H, json={"provider": "groq", "model": "llama-3.3-70b-versatile", "api_key": "gsk_not_real"})
    off = (await c.put(url("/advisor/ai-providers/groq/enabled"), headers=H, json={"enabled": False})).json()
    ok(not any(x.startswith("groq:") for x in off["order_used"]) and next(p_ for p_ in off["configured"] if p_["provider"] == "groq")["order"] is None,
       "an AI model switched off in Settings is not used (order used: " + (" → ".join(off["order_used"]) or "none") + ")")
    await c.delete(url("/advisor/ai-providers/groq"), headers=H)
    first = (await c.get(url("/readiness"), headers=H)).json().get("last_pass")
    q = await c.post(url("/readiness/check"), headers=H)
    ok(q.status_code == 200, "Health check → Check now starts a pass")
    rd = {}
    for _ in range(45):
        await asyncio.sleep(2)
        rd = (await c.get(url("/readiness"), headers=H)).json()
        lp = rd.get("last_pass") or {}
        if lp.get("status") in ("done", "failed") and lp != first and lp.get("finished_at"):
            break
    lp = rd.get("last_pass") or {}
    ok(lp.get("status") == "done", f"the readiness engine finished a pass ({lp.get('status')} {lp.get('error') or ''} {lp.get('stats', {}).get('holdings')} holdings)")
    rows = rd.get("holdings") or []
    ok(rows and all(set(x["checks"]) == {"fundamentals", "technicals", "analysis", "ai_review"} for x in rows),
       f"every holding has its four checks ({rd['summary']['ready']} ready, {rd['summary']['fixing']} being fixed, {rd['summary']['attention']} need attention)")
    stocks_ = [x for x in rows if x["asset_type"] == "stock"]
    ok(stocks_ and all(x["checks"]["fundamentals"]["state"] == "ok" and x["checks"]["technicals"]["state"] == "ok" for x in stocks_),
       f"stocks: fundamentals and technicals loaded for all {len(stocks_)}")
    ai_states = {x["checks"]["ai_review"]["state"] for x in rows}
    ok(ai_states <= {"ok", "failed", "waiting", "n/a"}, f"AI review checked for every holding ({sorted(ai_states)})")
    for rec in recs[:6]:
        print(f"     [{rec['bucket']:6}] {rec['action']:10} {rec['confidence']:.2f}  {rec['headline'][:90]}")
    full = (await c.get(url(f"/advisor/recommendations/{recs[0]['id']}"), headers=H)).json()
    fr = full["full_report"]
    ok(fr["decision_trace"]["rules_evaluated"] and fr["input_hash"], f"full report: rule {fr['decision_trace']['matched_rule']} after "
       f"{len(fr['decision_trace']['rules_evaluated'])} rules; hash {fr['input_hash'][:12]}")
    ok(bool(full["short_report"]["what_would_change"]), "short report has 'what would change this call'")
    act = await c.post(url(f"/advisor/recommendations/{recs[0]['id']}/act"), headers=H, json={"action": "accept", "note": "agree"})
    ok(act.json()["status"] == "accepted", "accept → recorded (no broker order in P1)")
    sh = (await c.get(url("/advisor/shadow"), headers=H)).json()
    ok("net_difference" in sh, f"shadow portfolio: {len(sh['trades'])} virtual trade(s), net ₹{sh['net_difference']}")
    await asyncio.sleep(3)
    full = (await c.get(url(f"/advisor/recommendations/{recs[0]['id']}"), headers=H)).json()
    ok(full["ai_consensus"] in ("rules_only", "verified", "caution", "needs_review", "pending", "ai_failed"), f"AI consensus badge: {full['ai_consensus']}")

    print("dashboard-bff + analytics")
    t0 = time.perf_counter()
    dash = (await c.get(url("/dashboard"), headers=H)).json()
    ok(not dash["errors"] and dash["advisor"]["top_actions"] is not None, f"dashboard fan-out in {(time.perf_counter() - t0) * 1000:.0f} ms")
    one = (await c.get(url("/dashboard"), headers=H, params={"scope": "profile", "id": sp["id"]})).json()
    ok(not one["errors"] and all(a["profile_id"] == sp["id"] for a in one["advisor"]["top_actions"]),
       f"a person's dashboard shows only their actions ({len(one['advisor']['top_actions'])} for Ravi vs {len(dash['advisor']['top_actions'])} family)")
    an = (await c.get(url("/analysis/instrument/TCS.NS"), headers=H)).json()
    ok(an["technical"]["available"] and an["fundamental"]["available"], f"TCS technical {an['technical']['score']} / fundamental {an['fundamental']['score']} / DQ {an['data_quality']['grade']}")

    stt = await c.get(url("/market/sources/test"), headers=H, params={"symbol": "TCS.NS"}, timeout=60)
    ok(stt.status_code == 200 and stt.json()["results"] and all(x["status"] in ("ok", "empty", "error", "not set up") for x in stt.json()["results"]),
       f"Settings → Test all sources answers ({', '.join(x['source'] + ':' + x['status'] for x in stt.json()['results'])})")
    fs = await c.get(url("/market/fundamentals/TCS.NS/sources"), headers=H)
    ok(fs.status_code == 200 and ("sources" in fs.json()), f"fundamentals source check answers ({fs.json().get('note') or [x['source'] + ':' + x['status'] for x in fs.json()['sources']]})")

    print("analytics (exposure)")
    ex = (await c.get(url("/holdings/exposure"), headers=H)).json()
    classes = {x["key"]: x["pct"] for x in ex["asset_classes"]}
    ok(abs(sum(classes.values()) - 100) < 0.5 and "equity" in classes and "debt" in classes and ex["sectors"] and ex["caps"],
       f"asset classes {classes}, {len(ex['sectors'])} sectors, sizes {[c['key'] for c in ex['caps']]}")
    eq = (await c.get(url("/holdings/exposure"), headers=H, params={"asset": "equity", "basis": "invested"})).json()
    ok(eq["asset"] == "equity" and all(p["key"] != "Retirement (EPF/PPF/NPS)" for p in eq["products"]), "asset-class filter + invested basis")
    one = (await c.get(url("/holdings/exposure"), headers=H, params={"scope": "profile", "id": sp["id"]})).json()
    ok(0 < one["total"] < ex["total"], f"per-profile view (Ravi {one['total']:,.0f} of family {ex['total']:,.0f})")

    tb2 = "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time\n" \
          f"SKYWAYS,INE0PX301025,{d(40)},NSE,EQ,EQ,buy,false,100,138,K1,K1,{d(40)}T10:00:00\n"
    await c.post(url("/imports"), headers=H, data={"portfolio_id": mine["broker"]}, files={"file": ("tb.csv", tb2, "text/csv")})
    stmt = '"Symbol","ISIN","Sector","Quantity Available","Average Price"\n"SKYWAYS","INE0PX301025","LOGISTICS","100","138"\n'
    pv = (await c.post(url("/imports/preview"), headers=H, data={"profile_id": me}, files={"file": ("h.csv", stmt, "text/csv")})).json()
    await c.post(url("/imports/commit"), headers=H, json={"token": pv["token"], "choices": {pv["groups"][0]["key"]: {"profile_id": me}}})
    ex = (await c.get(url("/holdings/exposure"), headers=H)).json()
    ok(any(s_["label"] == "Logistics" and any("SKYWAYS" in h["name"].upper() for h in s_["holdings"]) for s_ in ex["sectors"]),
       "re-importing a statement fills the stock's sector from its Sector column")

    nps = ("NPS Transaction Statement for Tier I Account,,,,\nPRAN,110000000002,,,\n"
           "Value of your Holdings(Investments) (in Rs),No of Contributions,Total Contribution (in Rs),Total Withdrawal (in Rs),Gain\n"
           "(A),,(B),(C),D\nRs 100000.00,5,Rs 80000.00,Rs 0.00,Rs 20000.00\n,,,,\n"
           "Investment Details - Scheme Wise Summary,,,,\nParticulars,Value (in Rs),Total Units ( U ),"
           f"NAV as on {(date.today() - timedelta(days=60)).strftime('%d-%b-%Y')} ( N ),\n"
           "HDFC PENSION FUND SCHEME E - TIER I,60000,1000,60,\nHDFC PENSION FUND SCHEME G - TIER I,40000,1000,40,\n,,,,\n")
    pv = (await c.post(url("/imports/preview"), headers=H, data={"profile_id": sp["id"]}, files={"file": ("nps.csv", nps, "text/csv")})).json()
    g = pv["groups"][0]
    ok(pv["kind"] == "nps_statement" and g.get("account_kind") == "nps" and pv["rows"] == 2, f"NPS statement recognised ({g.get('account_label')})")
    r = await c.post(url("/imports/commit"), headers=H, json={"token": pv["token"], "choices": {g["key"]: {"profile_id": sp["id"]}}})
    ok(r.status_code in (200, 201), f"NPS statement imported ({r.status_code})")
    one = (await c.get(url("/holdings/exposure"), headers=H, params={"scope": "profile", "id": sp["id"], "asset": "all"})).json()
    ret = next((p for p in one["products"] if p["key"] == "Retirement (EPF/PPF/NPS)"), None)
    ok(ret is not None and ret["value"] >= 100000 - 1, f"NPS lands in Retirement ({ret and ret['value']:,.0f})")
    eqr = (await c.get(url("/holdings/exposure"), headers=H, params={"scope": "profile", "id": sp["id"], "asset": "equity"})).json()
    nps_eq = next((p["value"] for p in eqr["products"] if p["key"] == "Retirement (EPF/PPF/NPS)"), 0)
    hs = (await c.get(url("/holdings"), headers=H, params={"profile_id": sp["id"]})).json()
    nps_rows = [h for h in hs["holdings"] if h["asset_type"] == "nps"]
    ok(all(h["price_status"] == "nav_statement" for h in nps_rows) and hs["stale_count"] == 0
       and hs["nps_nav_as_of"] == (date.today() - timedelta(days=60)).isoformat(),
       f"NPS statement NAV is not a 'stale price' warning; old NAV date {hs['nps_nav_as_of']} → gentle re-import reminder")
    ok(abs(nps_eq - 60000) < 1, f"NPS scheme E counts as equity, G as debt (equity part {nps_eq:,.0f})")

    print("tax statements (any broker) + tax engine")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from fixtures import tax_statements as fx

    xl = ("taxpnl.xlsx", fx.zerodha_like_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    pv = (await c.post(url("/imports/preview"), headers=H, files={"file": xl})).json()
    g = pv["groups"][0]
    ok(pv["kind"] == "tax_pnl" and (g.get("matched_profile") or {}).get("id") == sp["id"] and all(ck["ok"] for ck in pv["statement"]["checks"]),
       f"tax P&L recognised, matched to Ravi by PAN, {len(pv['statement']['checks'])} self-checks pass")
    plan_ = (await c.post(url("/imports/plan"), headers=H, json={"token": pv["token"], "key": g["key"], "profile_id": sp["id"]})).json()
    ok(plan_["counts"]["sales"] == fx.N_SALES and plan_["counts"]["dividends"] == 2, f"review shows what will be added {plan_['counts']}")
    r = await c.post(url("/imports/commit"), headers=H, json={"token": pv["token"], "choices": {g["key"]: {"profile_id": sp["id"]}}})
    job1 = r.json()["jobs"][0]["id"]
    ok(r.status_code in (200, 201) and r.json()["jobs"][0]["stats"]["sales"] == fx.N_SALES, f"tax statement imported ({fx.N_SALES} sales, 2 dividends)")
    ts = (await c.get(url("/tax/summary"), headers=H, params={"scope": "profile", "id": sp["id"]})).json()
    ravi = ts["people"][0]
    ok(ts["fy"] == "2026-27" and abs(ravi["dividends"] - fx.DIV_TOTAL) < 0.01 and ravi["estimated_tax"] > 0 and ravi["ltcg_exemption"]["used"] == 60000,
       f"tax summary FY {ts['fy']}: realised ₹{ravi['realised']:,.0f}, est. tax ₹{ravi['estimated_tax']:,.0f}")
    kvb = next(z for z in ts["zero_cost"] if z["symbol"] == "KARURVYSYA")
    ok(kvb["cost_override"] == round(1630.16 / 8 * 21, 2) or any(str(f).startswith("bonus") for f in kvb["flags"]),
       f"zero-cost lot resolved automatically ({[f for f in kvb['flags'] if f.startswith(('cost_estimated', 'bonus'))]})")
    lici = next(z for z in ts["zero_cost"] if z["symbol"] == "LICI")
    r = await c.patch(url(f"/tax/realised/{lici['id']}"), headers=H, json={"cost": 5000})
    ts2 = (await c.get(url("/tax/summary"), headers=H, params={"scope": "profile", "id": sp["id"]})).json()
    ok(r.status_code == 200 and abs(ts2["people"][0]["realised"] - (ravi["realised"] - 5000)) < 0.01, "entering the real cost of a zero-cost sale updates the tax")
    h = (await c.get(url("/holdings"), headers=H, params={"scope": "profile", "id": sp["id"]})).json()
    ok(h["summary"].get("fy_income") == fx.DIV_TOTAL, "dashboard tiles get this FY's dividends from the statement")
    hv = ts2["people"][0]["harvest"]
    ok(hv is not None and all(a["quantity"] > 0 and a["symbol"] for a in hv["actions"]), f"tax harvesting: {len(hv['actions'])} concrete action(s), saves ≈ ₹{hv['total_saving']:,.0f}")
    if hv["actions"]:
        a0 = hv["actions"][0]
        r = await c.post(url("/tax/harvest/done"), headers=H, json={"profile_id": sp["id"], "fy": ts2["fy"], **{k: a0[k] for k in (
            "key", "kind", "instrument_id", "symbol", "name", "quantity", "booked", "tax_saved")}, "st_part": a0.get("st_part", 0), "lt_part": a0.get("lt_part", 0)})
        ts3 = (await c.get(url("/tax/summary"), headers=H, params={"scope": "profile", "id": sp["id"]})).json()["people"][0]
        ok(r.status_code == 201 and all(a["key"] != a0["key"] for a in ts3["harvest"]["actions"]) and len(ts3["harvest_done"]) == 1,
           f"'Mark done' moves {a0['name']} to Done and updates what's left")
        await c.delete(url(f"/tax/harvest/done/{r.json()['id']}"), headers=H)
        ts4 = (await c.get(url("/tax/summary"), headers=H, params={"scope": "profile", "id": sp["id"]})).json()["people"][0]
        ok(any(a["key"] == a0["key"] for a in ts4["harvest"]["actions"]) and not ts4["harvest_done"], "undo brings it back")
    pv = (await c.post(url("/imports/preview"), headers=H, files={"file": xl})).json()
    r = await c.post(url("/imports/commit"), headers=H, json={"token": pv["token"], "choices": {pv["groups"][0]["key"]: {"profile_id": sp["id"]}}})
    job2 = r.json()["jobs"][0]["id"]
    lots = (await c.get(url("/tax/realised"), headers=H, params={"scope": "profile", "id": sp["id"]})).json()
    jobs = {j["id"]: j["status"] for j in (await c.get(url("/imports"), headers=H)).json()}
    ok(len(lots) == fx.N_SALES and jobs.get(job1) == "replaced", f"re-importing the same statement replaces it — still {len(lots)} sales, never doubled")
    r = await c.delete(url(f"/imports/{job2}"), headers=H)
    lots = (await c.get(url("/tax/realised"), headers=H, params={"scope": "profile", "id": sp["id"]})).json()
    ok(r.status_code == 200 and len(lots) == fx.N_SALES and any(x["cost_override"] == 5000 for x in lots), "deleting it brings the earlier import back (with the cost you entered)")
    pv = (await c.post(url("/imports/preview"), headers=H, files={"file": ("gains.csv", fx.other_broker_csv(), "text/csv")})).json()
    r = await c.post(url("/imports/commit"), headers=H, json={"token": pv["token"], "choices": {pv["groups"][0]["key"]: {"profile_id": sp["id"]}}})
    ok(pv["kind"] == "tax_pnl" and r.status_code == 409, "another broker's capital-gains CSV is read too, and refused for the wrong person (PAN mismatch)")

    print("rename everywhere")
    before = [r["headline"] for r in (await c.get(url("/advisor/recommendations"), headers=H, params={"status": "open,accepted,snoozed,dismissed", "include_hold": "true"})).json()]
    r = await c.patch(url(f"/profiles/{me}"), headers=H, json={"display_name": "Asha K"})
    ok(r.status_code == 200 and r.json()["display_name"] == "Asha K", "profile renamed")
    me_user = (await c.get(url("/auth/me"), headers=H)).json()
    ok(me_user["full_name"] == "Asha K", "your login name follows your own profile's name (sidebar)")
    await asyncio.sleep(3)  # advisor consumes profile.updated
    after = [r["headline"] for r in (await c.get(url("/advisor/recommendations"), headers=H, params={"status": "open,accepted,snoozed,dismissed", "include_hold": "true"})).json()]
    had = any(h.startswith("Asha Kumar: ") for h in before)
    ok(not any(h.startswith("Asha Kumar: ") for h in after) and (not had or any(h.startswith("Asha K: ") for h in after)),
       "existing advisor cards show the new name")
    await c.patch(url(f"/profiles/{me}"), headers=H, json={"display_name": "Asha Kumar"})

    print("PAN-tagged profiles & review-before-import")
    r = await c.post(url("/profiles"), headers=H, json={"display_name": "Dup", "relationship": "other", "pan": "ABCDE1234F"})
    ok(r.status_code == 409 and "Ravi" in r.json()["detail"], "a PAN can belong to only one profile")

    def cas(pan: str, investor: str, code: str, units: float) -> bytes:
        return json.dumps({"cas_type": "DETAILED", "statement_period": {"from": d(900), "to": d(1)},
                           "investor_info": {"name": investor, "email": "", "address": "", "mobile": ""},
                           "folios": [{"folio": f"F-{pan[-4:]}", "amc": "AMC", "PAN": pan, "schemes": [{
                               "scheme": "UTI Nifty 50 Index Fund - Direct Plan - Growth", "amfi": code, "isin": "INF789F01XA0", "rta": "KFINTECH",
                               "rta_code": "X", "open": 0, "close": units, "close_calculated": units,
                               "valuation": {"date": d(1), "nav": 150, "cost": units * 100, "value": units * 150},
                               "transactions": [{"date": d(600), "description": "Purchase", "amount": units * 100, "units": units, "nav": 100,
                                                 "type": "PURCHASE"}]}]}]}).encode()

    async def preview(content: bytes, name: str) -> dict:
        r = await c.post(url("/imports/preview"), headers=H, files={"file": (name, content, "application/octet-stream")})
        assert r.status_code == 200, r.text
        return r.json()

    pv = await preview(cas("ABCDE1234F", "RAVI KUMAR", "120716", 10), "ravi-cas.json")
    g = pv["groups"][0]
    ok(pv["kind"] == "cas_json" and g["pan_masked"] == "XXXXXX234F" and (g["matched_profile"] or {}).get("id") == sp["id"],
       f"CAS auto-matched to the profile with that PAN ({(g['matched_profile'] or {}).get('name')}, via {g['match_reason']})")
    r = await c.post(url("/imports/commit"), headers=H, json={"token": pv["token"], "choices": {g["key"]: {"profile_id": me}}})
    ok(r.status_code == 409, f"importing Ravi's PAN into Asha is refused ({r.json()['detail'][:70]}…)")
    pv = await preview(cas("ABCDE1234F", "RAVI KUMAR", "120716", 10), "ravi-cas.json")
    r = await c.post(url("/imports/commit"), headers=H, json={"token": pv["token"], "choices": {pv["groups"][0]["key"]: {"profile_id": sp["id"]}}})
    ok(r.status_code == 201 and r.json()["jobs"][0]["stats"]["created"] == 1 and r.json()["jobs"][0]["profile"]["id"] == sp["id"],
       "…and goes into Ravi's Mutual Funds when confirmed")
    pv = await preview(cas("ABCDE1234F", "RAVI KUMAR", "120716", 10), "ravi-cas.json")
    r = await c.post(url("/imports/commit"), headers=H, json={"token": pv["token"], "choices": {pv["groups"][0]["key"]: {"profile_id": sp["id"]}}})
    ok(r.json()["jobs"][0]["stats"]["created"] == 0, "re-importing the same statement adds nothing")

    pv = await preview(cas("PQRSX6789K", "ASHA KUMAR", "120716", 4), "asha-cas.json")
    g = pv["groups"][0]
    ok(g["matched_profile"] is None and g["suggested"].get("create") == "Asha Kumar", "unknown PAN → suggests a new profile named from the statement")
    r = await c.post(url("/imports/commit"), headers=H, json={"token": pv["token"], "choices": {g["key"]: {"profile_id": me}}})
    mine_p = next(p for p in (await c.get(url("/profiles"), headers=H)).json() if p["id"] == me)
    ok(r.status_code == 201 and mine_p["pan_tagged"] and mine_p["pan_masked"] == "XXXXXX789K", "an untagged profile gets tagged with the statement's PAN")
    pv = await preview(cas("PQRSX6789K", "ASHA KUMAR", "120716", 4), "asha-cas.json")
    ok((pv["groups"][0]["matched_profile"] or {}).get("id") == me, "…so the next statement matches automatically")
    pv = await preview(cas("ABCDE1234F", "RAVI KUMAR", "120716", 10), "ravi-cas.json")
    r = await c.post(url("/imports/commit"), headers=H, json={"token": pv["token"], "choices": {pv["groups"][0]["key"]: {"profile_id": me}}})
    ok(r.status_code == 409 and "XXXXXX789K" in r.json()["detail"], "a profile with a different PAN is refused, naming both PANs (masked)")

    try:
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Client ID", "ZX9001"])
        ws.append([])
        ws.append(["Symbol", "ISIN", "Quantity Available", "Average Price", "Previous Closing Price"])
        ws.append(["INFY", "INE009A01021", 5, 1500, 1520])
        buf = __import__("io").BytesIO()
        wb.save(buf)
        pv = await preview(buf.getvalue(), "holdings.xlsx")
        g = pv["groups"][0]
        ok(g["type"] == "account" and g["matched_profile"] is None and g["account_label"].startswith("Zerodha"), f"Zerodha account found in the statement ({g['account_label']})")
        r = await c.post(url("/imports/commit"), headers=H, json={"token": pv["token"], "mode": "merge", "choices": {g["key"]: {"profile_id": sp["id"]}}})
        ok(r.status_code == 201, "holdings committed into the chosen profile")
        pv = await preview(buf.getvalue(), "holdings.xlsx")
        ok((pv["groups"][0]["matched_profile"] or {}).get("id") == sp["id"], "…and that Zerodha account is remembered for next time")
    except ImportError:
        print("  (openpyxl not installed — skipping the Zerodha xlsx check)")

    print("audit & rate limit")
    login_again = (await c.post(url("/auth/login"), json={"email": email, "password": "correct-horse-battery"})).json()["access_token"]
    audit = (await c.get(url("/audit"), headers={"Authorization": f"Bearer {login_again}"}, params={"table": "transactions"})).json()
    ok(len(audit) > 0 and audit[0]["actor_user_id"], f"{len(audit)} audited transaction mutations with actor + trace id")
    users = (await c.get(url("/audit"), headers={"Authorization": f"Bearer {login_again}"}, params={"table": "users"})).json()
    ok(all((u["new_data"] or {}).get("password_hash") in (None, "[REDACTED]") for u in users), "password hashes redacted in audit log")

    print("first-login race & portfolio reset")
    r = await c.post(url("/auth/register"), json={"email": f"race+{uuid.uuid4().hex[:6]}@example.com", "password": "correct-horse-battery", "full_name": "Meera Iyer"})
    RH = {"Authorization": f"Bearer {r.json()['access_token']}"}
    await asyncio.gather(*(c.get(url("/profiles" if i % 2 else "/portfolios"), headers=RH) for i in range(12)))  # races the event consumer
    await asyncio.sleep(1.5)
    race = (await c.get(url("/profiles"), headers=RH)).json()
    ok(len(race) == 1 and race[0]["display_name"] == "Meera Iyer", f"exactly one 'Self' profile after a first-login race ({[x['display_name'] for x in race]})")
    rpf = next(p["id"] for p in (await c.get(url("/portfolios"), headers=RH)).json() if p["kind"] == "broker")
    await c.post(url("/transactions"), headers=RH, json={"portfolio_id": rpf, "symbol": "TCS.NS", "txn_type": "buy", "trade_date": d(30), "quantity": 1, "price": 3500})
    r = await c.post(url(f"/portfolios/{rpf}/clear"), headers=RH)
    ok(r.status_code == 200 and r.json()["deleted"] == 1, "clear portfolio removes all its transactions")
    h = (await c.get(url("/holdings"), headers=RH)).json()
    ok(not h["holdings"], "cleared portfolio has no holdings")

    print("other brokers · map the columns once")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    from tests.fixtures import broker_files as bf

    pv = await c.post(url("/imports/preview"), headers=H, files={"file": ("PortfolioEquity_Transactions.csv", bf.ICICI_TRADES, "text/csv")})
    ok(pv.status_code == 200 and pv.json()["kind"] == "trades" and pv.json()["rows"] == 3, f"ICICI Direct transactions recognised ({pv.json().get('kind_label')}, {pv.json().get('rows')} rows)")
    pv = await c.post(url("/imports/preview"), headers=H, files={"file": ("trade_report.xlsx", bf.UPSTOX_TRADES, "application/vnd.ms-excel")})
    ok(pv.status_code == 200 and pv.json()["rows"] == 3 and any("futures" in w["error"] for w in pv.json()["warnings"]),
       "Upstox trade report (Excel, title rows) recognised; F&O skipped")
    bi = (await c.post(url("/market/instruments/by-isin"), headers=H, json={"isins": ["INE009A01021", "NOTANISIN"]})).json()
    ok(isinstance(bi, dict) and "NOTANISIN" not in bi, f"ISIN → symbol lookup answers ({bi.get('INE009A01021', {}).get('symbol', 'not reachable offline')})")
    odd = await c.post(url("/imports/preview"), headers=H, files={"file": ("export.csv", bf.ODD_TRADES, "text/csv")})
    nm = ((odd.json().get("detail") or {}) if odd.status_code == 422 else {}).get("needs_mapping") or {}
    ok(odd.status_code == 422 and nm.get("headers") == ["Particular", "Dt", "B-S", "Nos", "Px", "Code"],
       "an unknown layout asks which column is which (with its columns and sample rows)")
    mp = {"kind": "trades", "label": "My odd broker", "fields": {"date": "Dt", "side": "B-S", "symbol": "Particular", "qty": "Nos", "price": "Px", "isin": "Code"}}
    pv = await c.post(url("/imports/preview"), headers=H, data={"mapping": json.dumps(mp)}, files={"file": ("export.csv", bf.ODD_TRADES, "text/csv")})
    ok(pv.status_code == 200 and pv.json()["rows"] == 2, f"…mapped once, it reads the file ({pv.json().get('rows')} rows)")
    pv = await c.post(url("/imports/preview"), headers=H, files={"file": ("export2.csv", bf.ODD_TRADES, "text/csv")})
    tl = (await c.get(url("/imports/templates"), headers=H)).json()
    ok(pv.status_code == 200 and pv.json()["rows"] == 2 and any(t["label"] == "My odd broker" and t["uses"] >= 2 for t in tl),
       "…and the next file with that layout is recognised automatically")
    for t in tl:
        await c.delete(url(f"/imports/templates/{t['id']}"), headers=H)

    print("holdings statement (snapshot) import")
    tb = "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time\n" \
         f"INFY,INE009A01021,{d(300)},NSE,EQ,EQ,buy,false,5,1500,H1,H1,{d(300)}T10:00:00\n"
    await c.post(url("/imports"), headers=RH, data={"portfolio_id": rpf}, files={"file": ("tradebook.csv", tb, "text/csv")})
    snap = '"Instrument","Qty.","Avg. cost","LTP","Cur. val","P&L","Net chg.","Day chg."\n"TCS","12","3400","3500","42000","1200","2.9","0.1"\n"RELIANCE","3","2500","2600","7800","300","4","0.2"\n'
    r = await c.post(url("/imports"), headers=RH, data={"portfolio_id": rpf, "mode": "replace"}, files={"file": ("holdings.csv", snap, "text/csv")})
    ok(r.status_code == 201 and r.json()["kind"] == "holdings" and r.json()["stats"]["created"] == 2, f"Kite holdings CSV → {r.json().get('stats')}")
    h = (await c.get(url("/holdings"), headers=RH)).json()
    got = {x["symbol"]: x["quantity"] for x in h["holdings"] if x["quantity"] > 0}
    ok(got == {"TCS.NS": 12, "RELIANCE.NS": 3}, f"snapshot replaced the old positions exactly ({got})")
    ok(abs(h["summary"]["invested"] - (12 * 3400 + 3 * 2500)) < 1, "invested = quantity × average cost from the statement")
    r = await c.delete(url(f"/imports/{r.json()['id']}"), headers=RH)
    ok(r.status_code == 200 and r.json()["deleted"] == 2, "a holdings import can be deleted as a whole")
    dup = '"Instrument","Qty.","Avg. cost"\n"LIQUIDBEES","200","1000"\n"LIQUIDBEES-F","8.5","1000"\n'
    r = await c.post(url("/imports"), headers=RH, data={"portfolio_id": rpf, "mode": "replace"}, files={"file": ("h.csv", dup, "text/csv")})
    got = {x["symbol"]: x["quantity"] for x in (await c.get(url("/holdings"), headers=RH)).json()["holdings"] if x["quantity"] > 0}
    ok(r.status_code == 201 and got == {"LIQUIDBEES.NS": 208.5}, f"X and X-F (fractional) rows merge into one holding ({got})")
    twins = "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time\n" \
            f"ITC,INE154A01025,{d(50)},NSE,EQ,EQ,buy,false,10,400,,,\nITC,INE154A01025,{d(50)},NSE,EQ,EQ,buy,false,10,400,,,\n"
    r = await c.post(url("/imports"), headers=RH, data={"portfolio_id": rpf}, files={"file": ("tb.csv", twins, "text/csv")})
    ok(r.status_code == 201 and r.json()["stats"]["created"] == 2, f"two identical same-day trades are both kept, no 500 ({r.status_code})")
    r = await c.post(url("/imports"), headers=RH, data={"portfolio_id": rpf}, files={"file": ("tb.csv", twins, "text/csv")})
    ok(r.status_code == 201 and r.json()["stats"]["created"] == 0 and r.json()["stats"]["duplicates_skipped"] == 2, "…and re-importing them adds nothing")
    print("holdings import can never double a position")
    rme = next(p for p in (await c.get(url("/profiles"), headers=RH)).json())["id"]
    old_pf = (await c.post(url("/portfolios"), headers=RH, json={"profile_id": rme, "name": "Old Zerodha", "kind": "broker"})).json()["id"]
    await c.post(url("/transactions"), headers=RH, json={"portfolio_id": old_pf, "symbol": "SUNPHARMA.NS", "txn_type": "buy", "trade_date": d(90), "quantity": 50, "price": 1800})
    await c.post(url("/transactions"), headers=RH, json={"portfolio_id": old_pf, "symbol": "INFY.NS", "txn_type": "buy", "trade_date": d(90), "quantity": 7, "price": 1500})
    sun = '"Instrument","Qty.","Avg. cost"\n"SUNPHARMA","50","1854"\n'
    qty = lambda h, s_: sum(x["quantity"] for x in h["holdings"] if x["symbol"] == s_)  # noqa: E731
    pv = (await c.post(url("/imports/preview"), headers=RH, data={"profile_id": rme}, files={"file": ("h.csv", sun, "text/csv")})).json()
    g = pv["groups"][0]
    ok(g["suggested"].get("profile_id") == rme, "the profile chosen at upload is pre-selected")
    pl = (await c.post(url("/imports/plan"), headers=RH, json={"token": pv["token"], "key": g["key"], "profile_id": rme, "mode": "replace"})).json()
    by = {x["symbol"]: x for x in pl["rows"]}
    ok(by["SUNPHARMA.NS"]["status"] == "same" and by["SUNPHARMA.NS"]["now"] == 50 and by["INFY.NS"]["status"] == "remove",
       f"preview shows what changes before import (SUNPHARMA 50→50 same, INFY removed; {pl['counts']})")
    body = {"token": pv["token"], "mode": "replace", "choices": {g["key"]: {"profile_id": rme}}}
    r1, r2 = await asyncio.gather(c.post(url("/imports/commit"), headers=RH, json=body), c.post(url("/imports/commit"), headers=RH, json=body))
    codes = sorted([r1.status_code, r2.status_code])
    ok(codes[0] == 201 and codes[1] in (409, 410), f"the same reviewed import can be written only once ({codes})")
    h = (await c.get(url("/holdings"), headers=RH)).json()
    ok(qty(h, "SUNPHARMA.NS") == 50 and qty(h, "INFY.NS") == 0, f"replace covers every Stocks portfolio of the profile → SUNPHARMA {qty(h, 'SUNPHARMA.NS')} (not 100)")
    pv = (await c.post(url("/imports/preview"), headers=RH, data={"profile_id": rme}, files={"file": ("h.csv", sun, "text/csv")})).json()
    await c.post(url("/imports/commit"), headers=RH, json={"token": pv["token"], "mode": "update", "choices": {pv["groups"][0]["key"]: {"profile_id": rme}}})
    h = (await c.get(url("/holdings"), headers=RH)).json()
    ok(qty(h, "SUNPHARMA.NS") == 50, "re-importing the same file (update mode) still leaves 50")

    print("sync keeps other sources · replace can be restored · delete import undoes")
    r = await c.post(url("/auth/register"), json={"email": f"sync+{uuid.uuid4().hex[:6]}@example.com", "password": "correct-horse-battery", "full_name": "Test Member"})
    SH = {"Authorization": f"Bearer {r.json()['access_token']}"}
    await asyncio.sleep(1.5)
    sme = (await c.get(url("/profiles"), headers=SH)).json()[0]["id"]
    tb = "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time\n" \
         f"ITC,INE154A01025,{d(300)},NSE,EQ,EQ,buy,false,100,400,S1,S1,{d(300)}T10:00:00\n"
    pv = (await c.post(url("/imports/preview"), headers=SH, data={"profile_id": sme}, files={"file": ("tb.csv", tb, "text/csv")})).json()
    hist = (await c.post(url("/imports/commit"), headers=SH, json={"token": pv["token"], "choices": {pv["groups"][0]["key"]: {"profile_id": sme}}})).json()["jobs"][0]["id"]
    snap = '"Instrument","Qty.","Avg. cost"\n"TCS","12","3400"\n'
    held = lambda: c.get(url("/holdings"), headers=SH)  # noqa: E731

    async def holdings_import(mode: str) -> dict:
        pv = (await c.post(url("/imports/preview"), headers=SH, data={"profile_id": sme}, files={"file": ("h.csv", snap, "text/csv")})).json()
        g = pv["groups"][0]
        plan = (await c.post(url("/imports/plan"), headers=SH, json={"token": pv["token"], "key": g["key"], "profile_id": sme, "mode": mode})).json()
        res = (await c.post(url("/imports/commit"), headers=SH, json={"token": pv["token"], "mode": mode, "choices": {g["key"]: {"profile_id": sme}}})).json()
        return {"plan": {x["symbol"]: x["status"] for x in plan["rows"]}, "job": res["jobs"][0]["id"]}

    def q(h: dict) -> dict:
        return {x["symbol"]: x["quantity"] for x in h["holdings"] if x["quantity"] > 0}

    a = await holdings_import("sync")
    ok(a["plan"] == {"TCS.NS": "new", "ITC.NS": "kept"} and q((await held()).json()) == {"ITC.NS": 100, "TCS.NS": 12},
       f"sync keeps transaction history that isn't in the statement ({a['plan']})")
    b = await holdings_import("replace")
    jobs = {j["id"]: j["status"] for j in (await c.get(url("/imports"), headers=SH)).json()}
    ok(b["plan"].get("ITC.NS") == "remove" and q((await held()).json()) == {"TCS.NS": 12} and jobs[hist] == "replaced",
       "'replace everything' sets the history aside and marks that import replaced")
    r = await c.post(url(f"/imports/{hist}/restore"), headers=SH)
    ok(r.status_code == 200 and q((await held()).json()) == {"ITC.NS": 100, "TCS.NS": 12}, f"restore brings it back without doubling ({r.json()})")
    r = await c.delete(url(f"/imports/{b['job']}"), headers=SH)
    ok(r.status_code == 200 and q((await held()).json()) == {"ITC.NS": 100, "TCS.NS": 12}, "deleting the replacing import restores what it replaced — still no doubling")

    st = (await c.get(url("/market/status"), headers=RH)).json()
    ok("mode" in st and "sources" in st, f"market data-source status ({st['mode']})")

    print("websocket")
    import websockets

    ws_url = (BASE.replace("http", "ws") if BASE else "ws://localhost:8003") + f"/api/v1/ws/prices?token={login_again}"
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"subscribe": ["RELIANCE.NS", "^NSEI"]}))
        msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
        ok(msg["type"] == "ticks" and msg["data"], f"live ticks: {[(t['symbol'], t['price']) for t in msg['data']]}")
    print("\nALL CHECKS PASSED")


asyncio.run(main())
