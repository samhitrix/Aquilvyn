"""Pages answer from the last good quote while it is recent; the source is asked in the background."""


def test_a_recent_last_quote_is_served_at_once_an_old_one_is_not():
    from datetime import UTC, datetime, timedelta

    from market_svc.quotes import RECENT_SECONDS, _age

    now = datetime.now(UTC)
    assert _age({"ts": (now - timedelta(minutes=5)).isoformat()}) < RECENT_SECONDS
    assert _age({"ts": (now - timedelta(hours=2)).isoformat()}) > RECENT_SECONDS
    assert _age({}) == float("inf")
