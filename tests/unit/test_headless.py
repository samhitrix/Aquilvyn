"""A fund house page that builds its download list with JavaScript: the plain download sees nothing,
the headless browser sees the files (from the rendered DOM and from the JSON the page loaded)."""
import asyncio
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from market_svc.fund_sources import extract_links, rank_links
from market_svc.providers import headless

PAGE = b"""<!doctype html><html><body><div id=root>Loading...</div><script>
fetch('/api/disclosures.json').then(r => r.json()).then(d => {
  document.getElementById('root').innerHTML = '<a href="' + d.latest + '">Monthly Portfolio</a>';
});
</script></body></html>"""
LIST = b'{"latest": "/files/Monthly-Portfolio-August-2026.xlsx", "older": ["/files/Monthly_Portfolio_July_2026.xlsx"]}'


def _browser_works() -> bool:
    if not headless.available():
        return False
    try:
        asyncio.run(headless.render("about:blank", seconds=20))
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _browser_works(), reason="no headless browser here (the Docker market image has one)")
def test_javascript_built_download_list_is_found(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "index.html").write_bytes(PAGE)
    (tmp_path / "api" / "disclosures.json").write_bytes(LIST)

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(tmp_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/"
    try:
        assert rank_links(extract_links(PAGE.decode(), base)) == []  # what a plain download sees
        status, html, final = asyncio.run(headless.render(base + "index.html", seconds=20))
        ranked = [u for u, _, _ in rank_links(extract_links(html, final))]
        assert status == 200
        assert ranked[0] == base + "files/Monthly-Portfolio-August-2026.xlsx"
        assert base + "files/Monthly_Portfolio_July_2026.xlsx" in ranked  # from the JSON the page loaded
    finally:
        srv.shutdown()
