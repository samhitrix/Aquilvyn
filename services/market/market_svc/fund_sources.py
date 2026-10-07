"""Find and download each fund house's latest monthly portfolio file (E16 look-through).

For one AMC: candidate pages come from ``fund_sources.json`` (editable) and, as a fallback, from AMFI's
portfolio-disclosure directory, which links to every fund house's own disclosure page. Each page is scanned
for links to Excel / zip files that look like a *monthly* portfolio; the newest-looking one is downloaded,
parsed (``fund_portfolio.parse_portfolio``) and matched to the schemes the family holds. Every step is
recorded in ``tried`` so `fm.py funds-check` / Settings can say exactly what failed and where.

``fetch`` is injected (an async ``url -> (status, bytes, final_url, content_type)``) so this is testable offline.
``render`` (optional, ``url -> (status, html, final_url)``) opens a page in a headless browser: used when the plain
download of a page finds no files, since many fund house sites build their download lists with JavaScript.
"""
from __future__ import annotations

import html
import json
import re
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from .fund_portfolio import MONTHS, acronyms, closest, code_of, match_schemes, month_end, parse_portfolio

Fetch = Callable[[str], Awaitable[tuple[int, bytes, str, str]]]
Render = Callable[[str], Awaitable[tuple[int, str, str]]]
SOURCES_FILE = Path(__file__).with_name("fund_sources.json")
FILE_EXT = r"(?:xlsx|xls|xlsm|zip|csv)"
LINK_RE = re.compile(r"""(?:href|src|data-url|data-href|url|file|link)\s*[=:]\s*["']([^"'<>\s]+?\.""" + FILE_EXT + r"""(?:\?[^"'<>\s]*)?)["']""", re.I)
BARE_RE = re.compile(r"""(https?://[^"'<>\s\\]+?\.""" + FILE_EXT + r"""(?:\?[^"'<>\s\\]*)?)""", re.I)
QUOTED_RE = re.compile(r"""["']((?:https?:)?/?/?[^"'<>\s\\]*?[^"'<>\s\\/]\.""" + FILE_EXT + r"""(?:\?[^"'<>\s\\]*)?)["']""", re.I)  # JSON values
ANCHOR_RE = re.compile(r"""<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", re.I | re.S)
WANT = re.compile(r"(?i)portfolio|holding")
SKIP = re.compile(r"(?i)fortnight|half[\s_-]?year|semi[\s_-]?annual|annual|riskometer|risk[\s_-]?o[\s_-]?meter|\bter\b|expense|factsheet|"
                  r"fact[\s_-]?sheet|voting|unclaimed|stewardship|sid\b|kim\b|addendum|notice|nav\b|dividend|idcw|overlap|"
                  r"top[\s_-]?10|derivative|ratings?\b|sebi[\s_-]?circular|presentation|brochure|leaflet")
MAX_CANDIDATES = 4
MAX_DOWNLOADS = 40  # per fund house per run (one-file-per-scheme houses need several)
FRESH_DAYS = 75  # a file older than this is used only if nothing newer can be found


def load_sources() -> dict[str, Any]:
    return json.loads(SOURCES_FILE.read_text(encoding="utf-8"))


def config_for(amc: str, sources: dict[str, Any] | None = None) -> dict[str, Any] | None:
    a = amc.lower()
    for cfg in (sources or load_sources())["amcs"]:
        if any(m in a for m in cfg.get("match", [])):
            return cfg
    return None


def link_period(text: str) -> tuple[int, int] | None:
    """(year, month) a file link / label refers to: "31Aug2026", "December_31_2025", "APRIL-2024", "Jul-26",
    "2026-07", "07-2026", "31.07.2026". Full dates first, so "December_31_2025" is never read as December 2031."""
    t = unquote(text).lower()
    t = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", re.sub(r"[_\-.'/,]+", " ", t))
    months = "|".join(MONTHS)
    mon = rf"((?:{months})[a-z]*)"
    for rx, order in ((rf"(?<!\d)\d{{1,2}}(?:st|nd|rd|th)? {mon} (20\d{{2}})(?!\d)", "my"),
                      (rf"\b{mon} \d{{1,2}}(?:st|nd|rd|th)? (20\d{{2}})(?!\d)", "my"),
                      (rf"\b{mon} (20\d{{2}})(?!\d)", "my"),
                      (r"(?<!\d)(?:0?[1-9]|[12]\d|3[01]) (0?[1-9]|1[0-2]) (20\d{2})(?!\d)", "ny"),
                      (r"(?<!\d)(?:0[1-9]|[12]\d|3[01])(0[1-9]|1[0-2])(20\d{2})(?!\d)", "ny"),  # 31072026
                      (r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)", "yn"),  # 20260731
                      (r"(?<!\d)(20\d{2}) ?(0[1-9]|1[0-2])(?!\d)", "yn"),
                      (r"(?<!\d)(0[1-9]|1[0-2]) ?(20\d{2})(?!\d)", "ny"),
                      (rf"\b{mon} (\d{{2}})(?!\d)", "my")):
        m = re.search(rx, t)
        if not m:
            continue
        a, b = m.group(1), m.group(2)
        if order == "my":
            if a[:3] not in MONTHS:
                continue
            y = int(b)
            return (y + 2000 if y < 100 else y, MONTHS[a[:3]])
        if order == "ny":
            return int(b), int(a)
        return int(a), int(b)
    return None


def extract_links(page: str, base_url: str) -> list[tuple[str, str]]:
    """(absolute url, label) of every spreadsheet / zip link on a page — anchors, data attributes, JSON blobs."""
    found: dict[str, str] = {}
    for href, inner in ANCHOR_RE.findall(page):
        label = re.sub(r"<[^>]+>", " ", inner)
        if re.search(r"\." + FILE_EXT + r"(\?|$)", href, re.I):
            found.setdefault(urljoin(base_url, html.unescape(href)), " ".join(label.split()))
    for rx in (LINK_RE, BARE_RE, QUOTED_RE):
        for u in rx.findall(page):
            found.setdefault(urljoin(base_url, html.unescape(u.replace("\\/", "/"))), "")
    return list(found.items())


def rank_links(links: list[tuple[str, str]]) -> list[tuple[str, str, tuple[int, int] | None]]:
    """Monthly-portfolio files first, newest month first; fortnightly / annual / factsheets etc. dropped."""
    keep = []
    for url, label in links:
        text = f"{label} {url.rsplit('/', 1)[-1]}"
        if SKIP.search(text):
            continue
        if not WANT.search(text) and not WANT.search(url):
            continue
        keep.append((url, label, link_period(text) or link_period(url)))
    keep.sort(key=lambda x: (x[2] is not None, x[2] or (0, 0)), reverse=True)
    return keep


def directory_pages(page: str, base_url: str, amc: str) -> list[str]:
    """Links on AMFI's portfolio-disclosure directory that point to this fund house's own page."""
    words = [w for w in re.sub(r"[^a-z ]", " ", amc.lower()).split() if w not in ("mutual", "fund", "india", "asset", "management")]
    out = []
    for href, inner in ANCHOR_RE.findall(page):
        label = " ".join(re.sub(r"<[^>]+>", " ", inner).lower().split())
        if words and all(w in label for w in words[:2]) and href.startswith(("http", "/")):
            out.append(urljoin(base_url, html.unescape(href)))
    return out[:3]


def template_urls(cfg: dict[str, Any], today: date) -> list[str]:
    """Direct file URL templates for the last two months (disclosures appear ~10 days after month end)."""
    out = []
    for back in (1, 2):
        y, m = today.year, today.month - back
        while m <= 0:
            y, m = y - 1, m + 12
        names = list(MONTHS)
        full = date(y, m, 1).strftime("%B")
        for t in cfg.get("files", []):
            out.append(t.format(yyyy=y, yy=f"{y % 100:02d}", mm=f"{m:02d}", mon=names[m - 1], Mon=names[m - 1].title(),
                                month=full.lower(), Month=full))
    return out


async def fetch_amc(amc: str, targets: dict[str, dict[str, str]], fetch: Fetch, *, today: date | None = None,
                    sources: dict[str, Any] | None = None, render: Render | None = None) -> dict[str, Any]:
    """Download + parse one fund house's latest monthly portfolio and match the given schemes.
    → {amc, ok, url, as_of, matched: {code: scheme dict + score}, unmatched: [codes], tried: [...], error}"""
    sources = sources or load_sources()
    cfg = config_for(amc, sources) or {"amc": amc, "pages": []}
    tried: list[dict[str, Any]] = []
    result: dict[str, Any] = {"amc": amc, "ok": False, "url": None, "as_of": None, "matched": {}, "unmatched": list(targets), "tried": tried, "error": None}

    candidates: list[tuple[str, str, Any]] = [(u, "template", None) for u in template_urls(cfg, today or date.today())]
    pages = list(cfg.get("pages", []))
    for page_url in pages + ["@amfi"]:
        if page_url == "@amfi":
            if candidates:
                break  # only fall back to AMFI's directory when the fund house's own pages gave nothing
            directory = sources.get("amfi_directory")
            if not directory:
                continue
            try:
                status, body, final, _ = await fetch(directory)
                tried.append({"step": "AMFI directory", "url": directory, "status": status})
                listed = directory_pages(body.decode("utf-8", "replace"), final, amc) if status < 400 else []
                if not listed and render:
                    try:
                        rstatus, html_, rfinal = await render(directory)
                        listed = directory_pages(html_, rfinal, amc)
                        d_entry: dict[str, Any] = {"step": "AMFI directory (browser)", "url": directory, "status": rstatus, "fund_house_pages": len(listed)}
                        if not listed:  # what the page does say about this fund house (for the report)
                            word = (re.sub(r"[^a-z ]", " ", amc.lower()).split() or [""])[0]
                            d_entry["mentions"] = [f"{' '.join(re.sub(r'<[^>]+>', ' ', t).split())[:60]} → {h[:120]}"
                                                   for h, t in ANCHOR_RE.findall(html_) if word and word in t.lower()][:3] or \
                                                  (f"'{word}' appears {html_.lower().count(word)}× but not as a link" if word else "")
                        tried.append(d_entry)
                    except Exception as exc:  # noqa: BLE001
                        tried.append({"step": "AMFI directory (browser)", "url": directory, "error": _short(exc)})
                if listed:
                    for p in listed:
                        if p not in pages:
                            candidates += await _scan_page(p, fetch, tried, render)
            except Exception as exc:  # noqa: BLE001
                tried.append({"step": "AMFI directory", "url": directory, "error": _short(exc)})
            continue
        candidates += await _scan_page(page_url, fetch, tried, render)

    if not candidates:
        result["error"] = ("no monthly portfolio file found on " + (", ".join(_host(p) for p in pages) or "AMFI's directory")
                           + (" — even in a browser" if render else " — the page may load its links with JavaScript")
                           + "; add the page or file URL pattern in fund_sources.json")
        return result

    tday = today or date.today()
    n_templates = len(template_urls(cfg, tday))
    queue = candidates[:n_templates] + order_candidates(candidates[n_templates:], targets)
    matched: dict[str, dict[str, Any]] = {}  # code → scheme (+ score, url): the newest seen, across files
    near: dict[str, tuple[str, float]] = {}
    downloads = 0
    for url, _label, period in queue:
        if downloads >= MAX_DOWNLOADS:
            break
        if period and all(c in matched and matched[c].get("as_of") and month_end(*period) and month_end(*period) <= matched[c]["as_of"]
                          for c in targets):
            continue  # older than what we already have for every fund
        downloads += 1
        try:
            status, body, final, _ctype = await fetch(url)
        except Exception as exc:  # noqa: BLE001
            tried.append({"step": "download", "url": url, "error": _short(exc)})
            continue
        if status >= 400 or not body:
            tried.append({"step": "download", "url": url, "status": status})
            continue
        if body[:15].lstrip().lower().startswith((b"<!doctype", b"<html")):
            tried.append({"step": "download", "url": url, "status": status, "error": "got a web page, not a spreadsheet"})
            continue
        fname = urlparse(final).path
        try:
            schemes = parse_portfolio(body, fname)
        except Exception as exc:  # noqa: BLE001
            tried.append({"step": "parse", "url": url, "error": _short(exc)})
            continue
        if not schemes:
            tried.append({"step": "parse", "url": url, "error": "no holdings table (ISIN + % to NAV columns) found"})
            continue
        file_date = month_end(*(link_period(fname.rsplit("/", 1)[-1]) or period or (0, 0)))
        for sc in schemes:  # the sheet didn't say: the file name's month
            sc["as_of"] = sc.get("as_of") or file_date
        m = match_schemes(schemes, targets, file_name=fname)
        entry: dict[str, Any] = {"step": "parse", "url": url, "schemes": len(schemes), "matched": len(m)}
        tried.append(entry)
        for code, v in m.items():
            sc = {**schemes[v["index"]], "score": v["score"], "url": final}
            have = matched.get(code)
            if have is None or (sc.get("as_of") or date.min) > (have.get("as_of") or date.min):
                matched[code] = sc
        if len(schemes) > 1:  # a whole-house file: remember the nearest names, for the report
            for code, t in targets.items():
                if code not in m:
                    top = closest(schemes, t, 1)
                    if top and (code not in near or top[0][1] > near[code][1]):
                        near[code] = top[0]
        if matched and all(c in matched and matched[c].get("as_of") and (tday - matched[c]["as_of"]).days <= FRESH_DAYS for c in targets):
            break
    if matched:
        dates = [x["as_of"] for x in matched.values() if x.get("as_of")]
        newest = max(dates) if dates else None
        url_of = {x["url"] for x in matched.values()}
        result.update(ok=True, url=next(iter(url_of)) if len(url_of) == 1 else f"{len(url_of)} files", matched=matched, as_of=newest,
                      unmatched=[c for c in targets if c not in matched])
        if not newest or (tday - newest).days > FRESH_DAYS:
            result["warning"] = f"newest file found is {'undated' if not newest else 'from ' + newest.strftime('%b %Y')}"
    else:
        result["error"] = "downloaded file(s) but none of your schemes were found in them" if any(t["step"] == "parse" for t in tried) \
            else "could not download the portfolio file"
    result["closest"] = {c: f"{n} (match {sc:.2f})" for c, (n, sc) in near.items() if c not in matched}
    return result


def order_candidates(ranked: list[tuple[str, str, Any]], targets: dict[str, dict[str, str]]) -> list[tuple[str, str, Any]]:
    """Every file of the newest month (one-file-per-scheme houses), those named with one of your funds' initials
    first; then a few older months as a fallback."""
    if not ranked:
        return []
    newest = ranked[0][2]
    group = [c for c in ranked if c[2] == newest] if newest else ranked[:MAX_CANDIDATES]
    rest = [c for c in ranked if c not in group][:MAX_CANDIDATES]
    acr = set().union(*(acronyms(t["name"], t.get("amc", "")) for t in targets.values())) if targets else set()
    group.sort(key=lambda c: code_of(c[0]) not in acr)  # stable: newest-first order kept otherwise
    return group + rest


async def _scan_page(url: str, fetch: Fetch, tried: list[dict[str, Any]], render: Render | None = None) -> list[tuple[str, str, Any]]:
    links: list[tuple[str, str, Any]] = []
    try:
        status, body, final, _ = await fetch(url)
        if status >= 400:
            tried.append({"step": "page", "url": url, "status": status})
        else:
            links = rank_links(extract_links(body.decode("utf-8", "replace"), final))
            tried.append({"step": "page", "url": url, "status": status, "files_found": len(links)})
    except Exception as exc:  # noqa: BLE001
        tried.append({"step": "page", "url": url, "error": _short(exc)})
    if links or not render:
        return links
    try:  # nothing in the plain HTML: open it in a real browser and let its JavaScript load the list
        status, html_, final = await render(url)
    except Exception as exc:  # noqa: BLE001
        tried.append({"step": "page (browser)", "url": url, "error": _short(exc)})
        return []
    links = rank_links(extract_links(html_, final))
    entry: dict[str, Any] = {"step": "page (browser)", "url": url, "status": status, "files_found": len(links)}
    tried.append(entry)
    if not links:  # download links that don't end in .xlsx ("/download?id=…"): try those labelled as a monthly portfolio
        loose = portfolio_anchors(html_, final)
        links = rank_links_loose(loose)
        entry["portfolio_links"] = [f"{lbl[:60]} → {u[:140]}" for u, lbl in loose[:5]] or "none"
    return links


def portfolio_anchors(page: str, base_url: str) -> list[tuple[str, str]]:
    """<a> elements whose text or address says 'portfolio' (any address but web pages / PDFs)."""
    out: dict[str, str] = {}
    for href, inner in ANCHOR_RE.findall(page):
        label = " ".join(re.sub(r"<[^>]+>", " ", inner).split())
        h = html.unescape(href)
        if h.startswith(("#", "javascript", "mailto")) or re.search(r"\.(pdf|html?|aspx|php|jpg|png)(\?|$)", h, re.I):
            continue
        text = f"{label} {h.rsplit('/', 1)[-1]}"
        if WANT.search(text) and not SKIP.search(text):
            out.setdefault(urljoin(base_url, h), label)
    return list(out.items())


def rank_links_loose(links: list[tuple[str, str]]) -> list[tuple[str, str, tuple[int, int] | None]]:
    """Like rank_links, for links without a file extension: only those that name a month (the download is sniffed)."""
    keep = [(u, lbl, link_period(f"{lbl} {u}")) for u, lbl in links]
    keep = [k for k in keep if k[2]]
    keep.sort(key=lambda x: x[2], reverse=True)
    return keep


def _host(url: str) -> str:
    return urlparse(url).netloc or url


def _short(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:160]}"
