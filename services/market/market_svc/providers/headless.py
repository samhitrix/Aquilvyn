"""A real (headless) Chromium for pages that build their content with JavaScript.

Many fund houses' disclosure pages are single-page apps: the HTML that a plain download gets is an empty
shell, and the list of files arrives later from the site's own JSON API. ``render`` opens the page like a
browser, waits for it to settle, and returns the final HTML **plus every file / JSON response the page
loaded**, so the usual link scan (``fund_sources.extract_links``) sees what a person would see.

Playwright is optional: the Docker market image ships it with Chromium; without it ``available()`` is
False and callers keep the plain-download behaviour. ``FUND_HEADLESS=off`` switches it off.
"""
from __future__ import annotations

import asyncio
import os
import re

from fm_common.logging import get_logger

log = get_logger(__name__)
FILE_URL = re.compile(r"\.(?:xlsx|xls|xlsm|zip|csv)(?:\?|$)", re.I)
MAX_JSON = 3_000_000
_sem = asyncio.Semaphore(2)  # at most two browsers at once (memory)


class HeadlessUnavailable(RuntimeError):
    pass


def available() -> bool:
    if os.environ.get("FUND_HEADLESS", "auto").lower() in ("off", "0", "false", "no"):
        return False
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


async def render(url: str, *, seconds: float = 45) -> tuple[int, str, str]:
    """→ (status, html + captured JSON / file URLs, final url). Raises HeadlessUnavailable if no browser."""
    if not available():
        raise HeadlessUnavailable("headless browser not installed (pip install playwright && python -m playwright install chromium)")
    from playwright.async_api import Error as PwError
    from playwright.async_api import async_playwright

    captured: list[str] = []

    async def on_response(resp) -> None:  # noqa: ANN001 — playwright Response
        try:
            if FILE_URL.search(resp.url):
                captured.append(f'<a href="{resp.url}">{resp.url}</a>')
                return
            ctype = (resp.headers or {}).get("content-type", "")
            if "json" in ctype and resp.ok:
                body = await resp.body()
                if len(body) <= MAX_JSON:
                    captured.append(body.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 — a response we can't read is just skipped
            return

    async with _sem, async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage",
                                                                    "--disable-blink-features=AutomationControlled"])
        except PwError as exc:
            raise HeadlessUnavailable(f"headless browser could not start: {str(exc).splitlines()[0][:160]}") from exc
        try:
            ctx = await browser.new_context(locale="en-IN", accept_downloads=False,
                                            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                                       "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
            await ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            await ctx.set_extra_http_headers({"Accept-Language": "en-IN,en;q=0.9"})
            page = await ctx.new_page()
            page.on("response", on_response)
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=seconds * 1000)
            status = resp.status if resp else 0
            try:
                await page.wait_for_load_state("networkidle", timeout=min(20, seconds) * 1000)
            except PwError:
                pass  # chatty pages never go idle; what has loaded so far is enough
            await _expand(page)
            html = await page.content()
            # links that only exist as JS click handlers / data attributes are in the DOM now; also hrefs of <a>
            hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => [e.href, e.innerText])")
            extra = "".join(f'<a href="{h}">{(t or "").strip()[:200]}</a>' for h, t in hrefs if h)
            return status, html + extra + "\n".join(captured), page.url
        finally:
            await browser.close()


async def _expand(page) -> None:  # noqa: ANN001
    """Click a 'Monthly portfolio' tab / accordion if the page has one (several AMC pages hide the list behind it)."""
    for text in ("Monthly Portfolio", "Monthly portfolio", "Month End Portfolio", "Portfolio Disclosure"):
        try:
            loc = page.get_by_text(text, exact=False).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=3000)
                await page.wait_for_timeout(2500)
                return
        except Exception:  # noqa: BLE001,S112 — optional nicety
            continue
