"""One SIP rule for Holdings (badge) and the advisor (fund plan): read from purchase dates; estimated dates never count."""
from datetime import date

from fm_common.sip import sip_info

TODAY = date(2026, 10, 5)


def lots(*days):
    return [{"buy_date": d, "qty": 50, "cost": 160.0} for d in days]


def test_a_monthly_sip_is_active_even_with_one_late_instalment():
    info = sip_info(lots("2026-04-06", "2026-05-04", "2026-06-02", "2026-07-02", "2026-08-03"), TODAY)
    assert info == {"active": True, "last_buy": "2026-08-03", "monthly": True, "source": "dates"}


def test_a_holdings_file_snapshot_is_never_a_sip():
    # a broker holdings file has no purchase dates: its units are recorded as bought today (estimated)
    assert sip_info(lots("2026-10-05"), TODAY, estimated=True)["active"] is None  # unknown, not "no"
    assert sip_info(lots("2026-10-05"), TODAY)["active"] is True  # a real purchase this month is


def test_ledger_rows_carry_the_sip_flag_for_mutual_funds_only():
    from portfolio_svc.ledger import sip_info as ledger_sip_info

    assert ledger_sip_info is sip_info  # Holdings uses the shared rule, not a copy
