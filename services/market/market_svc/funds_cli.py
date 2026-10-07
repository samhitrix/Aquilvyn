"""`python scripts/fm.py funds-check` — see, fund house by fund house, whether MF look-through data can be fetched.

    python -m market_svc.funds_cli check [--amc HDFC] [--no-force]   # live: fetch for the funds you hold, store, report
    python -m market_svc.funds_cli parse FILE                         # offline: what's inside a downloaded portfolio file
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):  # Windows consoles: never crash on ✔/→
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


def _print_parse(path: str) -> int:
    from .fund_portfolio import parse_portfolio

    content = Path(path).read_bytes()
    schemes = parse_portfolio(content, path)
    if not schemes:
        print(f"✘ no holdings table found in {path} — expected columns like 'ISIN' and '% to NAV / Net Assets'")
        return 1
    print(f"✔ {len(schemes)} scheme(s) in {Path(path).name}")
    for s in schemes:
        top = sorted(s["holdings"], key=lambda h: -h["weight"])[:3]
        print(f"  • {s['scheme']}  [sheet {s['sheet']}]  as of {s['as_of'] or '?'}  · {len(s['holdings'])} holdings · "
              f"equity {s['equity_pct']:.1f}% · total {s['total_pct']:.1f}%")
        print("      top: " + ", ".join(f"{h['name'][:28]} {h['weight']:.2f}%" for h in top))
    return 0


async def _check(amc: str | None, force: bool) -> int:
    from fm_common.db.session import SessionLocal

    from . import fund_holdings

    async with SessionLocal() as db:
        targets = await fund_holdings.held_targets(db)
        if not targets:
            print("No mutual funds / ETFs found to look through — import holdings first. (Needs AMFI's scheme list: "
                  "if that is unreachable, check Settings → Data sources → AMFI scheme list.)")
            return 1
        codes = {c for c, t in targets.items() if not amc or amc.lower() in t["amc"].lower()}
        print(f"Checking {len(codes)} fund(s) across {len({targets[c]['amc'] for c in codes})} fund house(s)…\n")
        report = await fund_holdings.refresh(db, codes=codes, force=force)
    bad = 0
    for r in report:
        _print_amc(r)
        bad += r.get("ok") is False
    print(f"\n{len(report) - bad} of {len(report)} fund house(s) OK.")
    if bad:
        print("Paste the ✘ lines above to your developer — or fix the page / file URL in services/market/market_svc/fund_sources.json.")
    return 1 if bad else 0


def _print_amc(r: dict[str, Any]) -> None:
    if r.get("skipped"):
        print(f"• {r['amc']}: {r['skipped']}" + (f" (portfolio as of {r['as_of']})" if r.get("as_of") else ""))
        return
    mark = "✔" if r.get("ok") else "✘"
    via = f" via {' + '.join(r['sources'])}" if r.get("sources") else ""
    print(f"{mark} {r['amc']}: " + (f"portfolio as of {r.get('as_of')}{via}" if r.get("ok") else (r.get("error") or "failed")))
    if r.get("ok") and r.get("url") and "fund house file" in (r.get("sources") or []):
        print(f"    file: {r['url']}")
    if r.get("warning"):
        print(f"    ! {r['warning']}")
    for code, m in (r.get("matched") or {}).items():
        src = f", {m['source']}" if m.get("source") else ""
        print(f"    ✔ {code} → {m['scheme']} (as of {m.get('as_of') or '?'}, {m['holdings']} holdings, equity {m['equity_pct']:.1f}%{src})")
    for u in r.get("unmatched") or []:
        print(f"    ✘ {u['code']} {u['name']}: not found" + (f" — {u['why']}" if u.get("why") else ""))
        if u.get("closest"):
            print(f"        nearest name in the file: {u['closest']}")
    if not r.get("ok") or r.get("unmatched"):
        for t in r.get("tried") or []:
            extra = ", ".join(f"{k}={v}" for k, v in t.items() if k not in ("step", "url"))
            print(f"      · {t['step']}: {t.get('url', '')} {extra}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="funds-check")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--amc", help="only this fund house (part of its name, e.g. HDFC)")
    c.add_argument("--no-force", action="store_true", help="only fetch what's due (the daily job's behaviour)")
    f = sub.add_parser("parse")
    f.add_argument("file")
    a = p.parse_args(argv)
    if a.cmd == "parse":
        return _print_parse(a.file)
    return asyncio.run(_check(a.amc, force=not a.no_force))


if __name__ == "__main__":
    sys.exit(main())
