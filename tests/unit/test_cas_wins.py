"""One source per mutual fund: a CAS (demat + non-demat, real history) wins over a broker holdings snapshot."""
import uuid
from types import SimpleNamespace

from portfolio_svc import fund_sources
from portfolio_svc.imports import rows_to_replace


def _t(symbol, source):
    return SimpleNamespace(id=uuid.uuid4(), symbol=symbol, source=source)


def test_match_keys_use_isin_and_scheme_code():
    assert fund_sources.keys_of("119062", "inf194k01y29") == {"code:119062", "isin:INF194K01Y29"}
    assert fund_sources.keys_of("119062", None) == {"code:119062"}
    snap, other = _t("999001", "import:z"), _t("123456", "import:z")
    keys = {snap.id: {"code:999001", "isin:INF194K01Y29"}, other.id: {"code:123456"}}
    # same ISIN, different scheme code (e.g. the snapshot was mapped to another plan's code): still the same fund
    assert fund_sources.covered({"isin:INF194K01Y29", "code:555"}, keys, [snap, other]) == [snap]


def test_a_holdings_snapshot_never_replaces_cas_rows():
    cas, snap, manual = _t("999001", "import:cas"), _t("999001", "import:old-snap"), _t("777", "manual")
    rows = [cas, snap, manual]
    assert rows_to_replace(rows, "replace", set(), set(), {"import:cas"}) == [snap, manual]
    assert rows_to_replace(rows, "sync", {"999001"}, set(), {"import:cas"}) == [snap]
    assert rows_to_replace(rows, "sync", {"999001"}, set()) == [cas, snap]  # without protection (the old bug)
