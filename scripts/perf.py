"""Where does page-load time go? Logs in as you, loads every page's API calls the way the browser does (in parallel),
twice (first load / repeat), and prints each call's time plus what the server waited on (its Server-Timing header:
the service's own work and each downstream service it called).

    python scripts/fm.py perf                        # no login: is every part up, and how fast does each answer?
    python scripts/fm.py perf --as you@example.com   # + every page's data, as that account (no password needed)
    python scripts/fm.py perf --login                # + every page's data, asks for email + password
    add --direct to go straight to the dev ports (python scripts/fm.py dev) instead of the gateway.

Without --as / --login it needs no account at all: it checks the gateway, the web app and every service's health
check (each with a 30 s limit), and shows what each service is busy with (CPU, the code that kept its event loop
stuck, its slowest routes), so it works even when the site doesn't open. Prints numbers only — no holdings,
names or amounts — so the output is safe to paste into an issue or a chat.
"""
from __future__ import annotations

import asyncio
import getpass
import os
import sys
import time

import httpx

DIRECT = {"auth": 8001, "members": 8001, "household": 8001, "profiles": 8002, "groups": 8002, "portfolios": 8002, "holdings": 8002,
          "transactions": 8002, "tax": 8002, "imports": 8002, "market": 8003, "analysis": 8004, "advisor": 8005, "dashboard": 8006,
          "readiness": 8007}
PAGES: dict[str, list[tuple[str, dict]]] = {
    "every page": [("/auth/me", {}), ("/profiles", {}), ("/groups", {}), ("/market/status", {})],
    "Dashboard": [("/dashboard", {}), ("/advisor/summary", {}), ("/market/overview", {})],
    "Holdings": [("/holdings", {}), ("/advisor/recommendations", {"status": "open,accepted,snoozed", "include_hold": "true"})],
    "Advisor": [("/advisor/recommendations", {"status": "open"}), ("/advisor/summary", {}), ("/readiness", {})],
    "Analytics": [("/holdings/exposure", {"lookthrough": "true"}), ("/advisor/recommendations", {"status": "open"})],
    "Tax": [("/tax/summary", {}), ("/tax/realised", {})],
    "Transactions": [("/transactions", {"limit": 300}), ("/portfolios", {})],
    "Markets": [("/market/overview", {})],
}


def base_for(path: str, direct: bool, base: str) -> str:
    if not direct:
        return f"{base}/api/v1{path}"
    return f"http://localhost:{DIRECT[path.strip('/').split('/')[0]]}/api/v1{path}"


def timing(header: str) -> str:
    """'app;dur=812.3, market;dur=640.1;desc="3 calls"' → 'own 172 · market 640 (3 calls)'."""
    parts = []
    app = 0.0
    waited = 0.0
    for item in [x.strip() for x in header.split(",") if x.strip()]:
        bits = item.split(";")
        name = bits[0]
        dur = next((float(b.split("=", 1)[1]) for b in bits[1:] if b.startswith("dur=")), 0.0)
        desc = next((b.split("=", 1)[1].strip('"') for b in bits[1:] if b.startswith("desc=")), "")
        if name == "app":
            app = dur
        else:
            waited += dur
            parts.append(f"{name} {dur:.0f}" + (f" ({desc})" if desc else ""))
    return " · ".join([f"server {app:.0f}"] + (["waited on: " + ", ".join(parts)] if parts else []))


HEALTH = ["identity", "portfolio", "market", "analytics", "advisor", "dashboard", "readiness"]
DIRECT_PORTS = {"identity": 8001, "portfolio": 8002, "market": 8003, "analytics": 8004, "advisor": 8005, "dashboard": 8006, "readiness": 8007}


async def infra(base: str, direct: bool) -> bool:
    """No login needed: is each part up, and how fast? → True when everything answered."""
    checks: list[tuple[str, str]] = []
    if not direct:
        checks += [("gateway (nginx)", f"{base}/health"), ("web app: login page", f"{base}/login"),
                   ("API through the gateway", f"{base}/api/v1/auth/sso")]
        checks += [(f"{svc} service", f"{base}/health/{svc}") for svc in HEALTH]
    else:
        checks += [(f"{svc} service", f"http://localhost:{port}/health/ready") for svc, port in DIRECT_PORTS.items()]
        checks += [("web app: login page", "http://localhost:3000/login")]

    async def one(c: httpx.AsyncClient, name: str, url: str) -> tuple[str, float, str]:
        s = time.perf_counter()
        try:
            r = await c.get(url)
            ok = "ok" if r.status_code < 400 else f"HTTP {r.status_code}"
            detail = ""
            if url.endswith(("/health/ready",)) or "/health/" in url:
                try:
                    body = r.json()
                    bad = [k for k, v in (body.get("checks") or {}).items() if v not in ("ok", True)]
                    detail = f" (failing: {', '.join(bad)})" if bad else ""
                except ValueError:
                    pass
            return name, (time.perf_counter() - s) * 1000, ok + detail
        except httpx.TimeoutException:
            return name, (time.perf_counter() - s) * 1000, "NO ANSWER in 30 s"
        except httpx.HTTPError as exc:
            return name, (time.perf_counter() - s) * 1000, f"DOWN ({type(exc).__name__})"

    print("=== Is every part up? (no login) ===")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        res = await asyncio.gather(*(one(c, n, u) for n, u in checks))
    for name, ms, status in res:
        flag = "  " if status.startswith("ok") and ms < 2000 else "!!"
        print(f" {flag} {name:<26} {ms:7.0f} ms  {status}")
    bad = [r for r in res if not r[2].startswith("ok") or r[1] >= 2000]
    print("\nAll parts answered quickly." if not bad else f"\n{len(bad)} part(s) slow or not answering — marked !! above.")
    return not bad


async def busy(base: str, direct: bool) -> None:
    """No login needed: what each service process is busy with — CPU, where its event loop was stuck, slowest routes."""
    urls = {svc: (f"http://localhost:{port}/health/stats" if direct else f"{base}/health/{svc}/stats") for svc, port in DIRECT_PORTS.items()}

    async def one(c: httpx.AsyncClient, svc: str, url: str) -> tuple[str, dict | str]:
        try:
            r = await c.get(url)
            return svc, (r.json() if r.status_code == 200 else f"HTTP {r.status_code}")
        except (httpx.HTTPError, ValueError) as exc:
            return svc, type(exc).__name__

    async with httpx.AsyncClient(timeout=30) as c:
        res = await asyncio.gather(*(one(c, s, u) for s, u in urls.items()))
    print("\n=== What each service is busy with (since it started) ===")
    for svc, data in res:
        if isinstance(data, str):
            print(f"   {svc:<16} no report ({data}) — rebuild the images to get it (python scripts/fm.py up-lite)")
            continue
        for p in data.get("processes") or []:
            loop = p.get("loop") or {}
            cpu = p.get("cpu_pct_1m")
            flag = "!!" if (cpu or 0) >= 50 or loop.get("blocked_ms", 0) >= 5000 else "  "
            print(f" {flag} {p.get('name') or svc:<16} pid {p.get('pid')}: CPU {cpu if cpu is not None else '?'}% (last minute) · "
                  f"{p.get('cpu_s_total')} CPU-s in {p.get('uptime_s')} s · {p.get('threads')} threads · "
                  f"event loop stuck {loop.get('blocked_ms', 0) / 1000:.1f} s in total (worst {loop.get('max_lag_ms', 0)} ms)")
            for t in (loop.get("top") or [])[:4]:
                inside = f"  → in {t['inside'][0]}" if t.get("inside") and t["inside"][0] != t["where"] else ""
                print(f"        stuck {t['ms'] / 1000:6.1f} s at {t['where']}{inside}")
            for r in (p.get("routes") or [])[:4]:
                print(f"        {r['route']:<44} ×{r['n']:<5} total {r['total_ms'] / 1000:7.1f} s · slowest {r['max_ms']:.0f} ms")
            if p.get("inflight"):
                print("        running now: " + ", ".join(f"{r['route']} ({r['age_ms'] / 1000:.0f} s)" for r in p["inflight"][:4]))


async def main() -> int:
    direct = "--direct" in sys.argv
    base = os.environ.get("BASE", "http://localhost:8080").rstrip("/")
    healthy = await infra(base, direct)
    await busy(base, direct)
    token = os.environ.get("FM_PERF_TOKEN")  # `fm.py perf --as you@example.com`: no password needed
    if not token and "--login" not in sys.argv:
        if healthy:
            print("For every page's timings too: python scripts/fm.py perf --as you@example.com")
        print("\nPaste this output into the chat / an issue — it has timings only, no personal data.")
        return 0 if healthy else 1
    async with httpx.AsyncClient(timeout=600) as c:
        if not token:
            print("(Rather not type a password? Run: python scripts/fm.py perf --as you@example.com)")
            email = os.environ.get("FM_PERF_EMAIL") or input("Login email: ").strip()
            password = os.environ.get("FM_PERF_PASSWORD") or getpass.getpass("Password: ")
            r = await c.post(base_for("/auth/login", direct, base), json={"email": email, "password": password})
            if r.status_code != 200:
                print(f"Login failed: HTTP {r.status_code} — no password? use: python scripts/fm.py perf --as {email or 'you@example.com'}")
                return 1
            token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        async def one(path: str, params: dict) -> tuple[str, float, int, str, int]:
            s = time.perf_counter()
            try:
                resp = await c.get(base_for(path, direct, base), headers=headers, params=params)
                return path, (time.perf_counter() - s) * 1000, resp.status_code, resp.headers.get("server-timing", ""), len(resp.content)
            except httpx.HTTPError as exc:
                return path, (time.perf_counter() - s) * 1000, 0, type(exc).__name__, 0

        slow: list[tuple[float, str, str, str]] = []
        for round_ in ("first load", "repeat"):
            print(f"\n=== {round_} ===")
            for page, calls in PAGES.items():
                s = time.perf_counter()
                res = await asyncio.gather(*(one(p, q) for p, q in calls))
                print(f"{page:<13} ready in {(time.perf_counter() - s) * 1000:7.0f} ms")
                for path, ms, status, st, size in res:
                    print(f"    {path:<28} {ms:7.0f} ms  HTTP {status:<3} {size // 1024:>4} KB   {timing(st) if st and '=' in st else st}")
                    slow.append((ms, page, path, timing(st) if st and "=" in st else st))
        try:
            status = (await c.get(base_for("/market/status", direct, base), headers=headers)).json()
            failing = [k for k, v in (status.get("sources") or {}).items() if (v or {}).get("status") == "failing"]
            print(f"\nData sources failing right now: {', '.join(failing) if failing else 'none'}")
        except Exception:  # noqa: BLE001
            pass
        print("\nSlowest calls:")
        for ms, page, path, st in sorted(slow, reverse=True)[:8]:
            print(f"  {ms:7.0f} ms  {page:<13} {path:<28} {st}")
        print("\nPaste this output into the chat / an issue — it has timings only, no personal data.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
