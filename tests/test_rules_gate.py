from decimal import Decimal

from analytics.risk.rules_gate import Limits, check_trade, limits_from_env

LIM = Limits(equity=Decimal("1330"), cash=Decimal("1330"))


def test_five_wide_spread_fails_width_and_hard_risk():
    r = check_trade("AMZN", max_loss=400, width=5, limits=LIM)
    assert not r.ok
    s = r.summary()
    assert s.startswith("❌") and "width 5 > 1" in s and "30.1% of equity" in s


def test_one_wide_passes_hard_cap_but_warns_over_2pct():
    r = check_trade("PLTR", max_loss=60, width=1, limits=LIM)
    assert r.ok and r.summary().startswith("⚠️") and "hard stop" in r.summary()
    assert check_trade("PLTR", max_loss=20, width=1, limits=LIM).summary() == "✅ rules"


def test_naked_short_banned_underlying_and_cash():
    r = check_trade("SNDQ", max_loss=None, cash_needed=9500, limits=LIM)
    text = " ".join(r.lines())
    assert "naked" in text and "banned" in text and "rule 6" in text and not r.ok


def test_limits_from_env_and_missing_equity_warning():
    lim = limits_from_env({"ACCOUNT_EQUITY": "2000", "RULE_MAX_WIDTH": "2.5", "BANNED_UNDERLYINGS": "abcd, xyz"})
    assert lim.equity == Decimal("2000") and lim.max_width == Decimal("2.5") and "ABCD" in lim.banned
    r = check_trade("AMZN", max_loss=50, width=1, limits=Limits())
    assert "ACCOUNT_EQUITY" in r.summary()
    assert limits_from_env({"ACCOUNT_EQUITY": "abc"}).equity is None
