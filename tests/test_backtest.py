from datetime import date, timedelta
from decimal import Decimal

import pytest

from analytics.backtest.bars import Bar, load_csv
from analytics.backtest.engine import run_backtest
from analytics.backtest.metrics import compute_metrics, format_report
from analytics.backtest.rules import RuleSpec, mean_reversion, trend_following


def _bars(opens, closes=None):
    closes = closes or opens
    d0 = date(2026, 1, 1)
    return [Bar(d0 + timedelta(days=i), Decimal(str(o)), Decimal(str(c))) for i, (o, c) in enumerate(zip(opens, closes))]


def _scripted(entry_idx, exit_idx, size="1"):
    return RuleSpec("scripted", "t", "e", "x", entry=lambda b, i: i in entry_idx,
                    exit=lambda b, i: i in exit_idx, size_fraction=Decimal(size))


def test_fills_at_next_open_never_same_bar():
    # signal at close of bar 1 (close 50); fill must use bar 2 open (60), exit at bar 4 open (80)
    bars = _bars([100, 100, 60, 70, 80, 80], [100, 50, 65, 75, 85, 85])
    r = run_backtest(bars, _scripted({1}, {3}), capital=6000, slippage_bps=0)
    (t,) = r.trades
    assert (t.entry_price, t.exit_price, t.shares) == (60, 80, 100)
    assert t.entry_day == bars[2].day and t.exit_day == bars[4].day
    assert t.pnl == Decimal("2000") and not t.forced


def test_costs_reduce_pnl_and_open_position_is_force_closed():
    bars = _bars([10, 10, 10, 10], [10, 10, 10, 10])
    r = run_backtest(bars, _scripted({0}, set()), capital=1000, fee_per_fill=1, slippage_bps=100)
    (t,) = r.trades
    assert t.forced and t.pnl < 0
    assert r.equity[-1][1] < 1000


def test_never_entry_before_signal_and_skips_if_cannot_afford():
    bars = _bars([5000, 5000, 5000])
    r = run_backtest(bars, _scripted({0}, set()), capital=100)
    assert r.trades == ()


def test_metrics_flag_small_sample_and_loss_dominance():
    opens = [100, 100, 100, 100, 110, 110, 110, 60, 60]
    bars = _bars(opens)
    r = run_backtest(bars, _scripted({1, 5}, {3, 6}), capital=10000, slippage_bps=0)
    m = compute_metrics(r)
    assert m.trades == 2
    assert any("noise" in w for w in m.warnings)
    assert m.win_rate == 0.5
    assert m.wins_erased_by_one_loss > 1
    assert any("erases" in w for w in m.warnings)
    assert "Loss erases" in format_report(r, m)


def test_drawdown_and_exposure():
    bars = _bars([100, 100, 50, 50], [100, 100, 50, 50])
    r = run_backtest(bars, _scripted({0}, set()), capital=1000, slippage_bps=0)
    m = compute_metrics(r)
    assert m.max_drawdown_pct == pytest.approx(50.0)
    assert m.exposure_pct == pytest.approx(75.0)


def test_mean_reversion_enters_on_selloff_exits_above_mean():
    flat = [100.0] * 20
    closes = flat + [80.0, 90.0, 105.0, 105.0, 105.0]
    r = run_backtest(_bars(closes), mean_reversion(), capital=10000, slippage_bps=0)
    assert len(r.trades) == 1
    assert r.trades[0].entry_price == Decimal("90.0")  # open after the 80 close
    assert r.trades[0].pnl > 0


def test_trend_following_enters_on_breakout():
    closes = [100.0] * 20 + [120.0, 125.0, 90.0, 90.0]
    r = run_backtest(_bars(closes), trend_following(), capital=10000, slippage_bps=0)
    assert r.trades and r.trades[0].entry_day == date(2026, 1, 1) + timedelta(days=21)


def test_system_sentence_and_csv_loader(tmp_path):
    s = mean_reversion().system_sentence()
    assert s.startswith("When close is below the lower Bollinger Band")
    p = tmp_path / "x.csv"
    p.write_text("Date,Open,High,Close\n2026-01-02,10,11,10.5\n2026-01-01,9,10,9.5\n")
    bars = load_csv(p)
    assert [b.close for b in bars] == [Decimal("9.5"), Decimal("10.5")]
    p.write_text("date,open,close\n2026-01-01,1,1\n2026-01-01,1,1\n")
    with pytest.raises(ValueError):
        load_csv(p)


def test_fetch_bars_falls_back_and_caches(tmp_path, monkeypatch):
    from analytics.backtest import data

    def boom(*a):
        raise RuntimeError("no creds")

    good = [Bar(date(2026, 1, 2), Decimal("2"), Decimal("3")), Bar(date(2026, 1, 1), Decimal("1"), Decimal("2"))]
    monkeypatch.setitem(data.PROVIDERS, "longbridge", boom)
    monkeypatch.setitem(data.PROVIDERS, "yfinance", lambda *a: good)
    bars, used = data.fetch_bars("AAPL", date(2026, 1, 1), date(2026, 2, 1))
    assert used == "yfinance"
    data.save_csv(sorted(bars, key=lambda b: b.day), tmp_path / "a.csv")
    assert load_csv(tmp_path / "a.csv")[0].open == Decimal("1")
    monkeypatch.setitem(data.PROVIDERS, "yfinance", boom)
    with pytest.raises(RuntimeError, match="no price data"):
        data.fetch_bars("AAPL", date(2026, 1, 1), date(2026, 2, 1))


def test_longbridge_symbol_and_bar_normalising():
    from analytics.backtest.data import _to_bars, longbridge_symbol
    assert longbridge_symbol("aapl") == "AAPL.US" and longbridge_symbol("700.HK") == "700.HK"
    bars = _to_bars([(date(2026, 1, 2), 2, 3), (date(2026, 1, 1), 1, 2), (date(2026, 1, 3), 0, 0)])
    assert [b.day.day for b in bars] == [1, 2]


# ------------------------- stage 2 / 3: option backtests ------------------------- #
def _daily(n, price_fn, start=date(2026, 1, 5)):
    bars, d = [], start
    while len(bars) < n:
        if d.weekday() < 5:
            p = price_fn(len(bars))
            bars.append(Bar(d, Decimal(str(round(p, 2))), Decimal(str(round(p, 2)))))
        d += timedelta(days=1)
    return bars


def test_bs_parity_and_delta_strike_round_trip():
    from analytics.backtest.pricing import bs_price, strike_for_delta
    from analytics.screening.screener import _bs_delta
    c, p = bs_price(100, 100, 0.25, 0.3, True), bs_price(100, 100, 0.25, 0.3, False)
    assert c - p == pytest.approx(100 - 100 * 2.718281828 ** (-0.045 * 0.25), abs=1e-6)
    k = strike_for_delta(100, 35 / 365, 0.3, 0.2, is_call=False)
    assert abs(_bs_delta(100, k, 35, 0.3, False)) == pytest.approx(0.2, abs=0.01)


def test_modelled_put_credit_expires_worthless_on_flat_market():
    from analytics.backtest.option_engine import SpreadStructure, run_option_backtest
    from analytics.backtest.rules import weekly_income
    bars = _daily(90, lambda i: 100 + (0.2 if i % 2 else 0))
    r = run_option_backtest(bars, weekly_income(), SpreadStructure("put_credit", width=2),
                            capital=20000, stop_multiple=None, profit_target=None)
    assert r.pricing == "modelled" and r.trades
    assert all(t.pnl > 0 for t in r.trades if t.note == "expiry")
    assert compute_metrics(r).warnings


def test_modelled_crash_loses_at_most_width_and_risk_budget_gate_skips():
    from analytics.backtest.option_engine import SpreadStructure, run_option_backtest
    from analytics.backtest.rules import weekly_income
    bars = _daily(80, lambda i: 100 if i < 40 else 60)
    st = SpreadStructure("put_credit", width=2)
    r = run_option_backtest(bars, weekly_income(), st, capital=20000, risk_pct=0.05, fee_per_contract=0,
                            stop_multiple=None, profit_target=None, slippage_per_share=0)
    worst = min(t.pnl for t in r.trades)
    assert worst >= -Decimal(2 * 100 * 10)  # never beyond width x 100 x contracts(<=10)
    tiny = run_option_backtest(bars, weekly_income(), st, capital=1330, risk_pct=0.02)
    assert not tiny.trades and any("risk budget" in k for k, _ in tiny.skipped)


def _snap_quotes(ticker, expiry, spot, day):
    from datetime import datetime, timezone
    from analytics.options.option_chain import OptionChainSnapshot, OptionContract, OptionQuote
    qs = []
    for k, bid, ask, delta in [(95, "1.90", "2.10", "-0.20"), (93, "1.20", "1.40", "-0.12")]:
        qs.append(OptionQuote(OptionContract(f"{ticker}P{k}", ticker, expiry, k, "put"),
                              datetime(day.year, day.month, day.day, 14, tzinfo=timezone.utc), "delayed",
                              bid=bid, ask=ask, open_interest=500, delta=delta, implied_volatility="0.3"))
    return OptionChainSnapshot(ticker, spot, datetime(day.year, day.month, day.day, 14, tzinfo=timezone.utc), "delayed", tuple(qs))


def test_snapshot_pricing_uses_real_quotes_skips_missing_and_settles_at_expiry(tmp_path):
    from analytics.backtest.option_engine import SpreadStructure, run_option_backtest
    from analytics.backtest.rules import weekly_income
    from analytics.backtest.snapshots import SnapshotLibrary, save_snapshot
    bars = _daily(40, lambda i: 100)
    fills = [b for b in bars if b.day.weekday() == 0][1:3]   # two Monday fills
    lib = SnapshotLibrary(tmp_path)
    save_snapshot(_snap_quotes("XYZ", fills[0].day + timedelta(days=35), 100, fills[0].day), tmp_path)
    r = run_option_backtest(bars, weekly_income(), SpreadStructure("put_credit", width=2, dte=35),
                            ticker="XYZ", pricing="snapshot", capital=20000, snapshots=lib,
                            stop_multiple=None, profit_target=None, fee_per_contract=0)
    assert r.pricing == "snapshot"
    assert r.trades and r.trades[0].entry_price == Decimal("0.5")   # sell bid 1.90, buy ask 1.40
    assert any(k == "no snapshot" for k, _ in r.skipped)
    assert "snapshot" in format_report(r, compute_metrics(r))


def test_snapshot_library_round_trip_and_listing(tmp_path):
    from analytics.backtest.snapshots import SnapshotLibrary, save_snapshot
    snap = _snap_quotes("ABC", date(2026, 3, 20), 100, date(2026, 2, 13))
    save_snapshot(snap, tmp_path)
    lib = SnapshotLibrary(tmp_path)
    assert lib.days("abc") == [date(2026, 2, 13)]
    assert lib.get("ABC", date(2026, 2, 13)).snapshot_id == snap.snapshot_id
    assert lib.get("ABC", date(2026, 2, 14)) is None


def test_profit_factor_and_expectancy():
    bars = _bars([100, 100, 100, 100, 110, 110, 110, 60, 60])
    m = compute_metrics(run_backtest(bars, _scripted({1, 5}, {3, 6}), capital=10000, slippage_bps=0))
    assert m.profit_factor == pytest.approx(0.2)   # +1000 gain vs -5000 loss
    assert m.expectancy == pytest.approx(-2000.0)


def test_delayed_rule_shifts_signals_one_bar():
    from analytics.backtest.robust import delayed
    d = delayed(_scripted({2}, {4}))
    assert [i for i in range(6) if d.entry([], i)] == [3]
    assert [i for i in range(6) if d.exit([], i)] == [5]


def test_robust_report_runs_and_flags_fragile_island():
    import math
    from analytics.backtest.robust import Cell, is_fragile, report, split
    bars = _bars([100 + 10 * math.sin(i / 4) + i * 0.05 for i in range(300)])
    out = report(bars, mean_reversion)
    for needle in ("Benchmark", "Delay 1 bar", "Grid best", "Neighbours", "Train/test"):
        assert needle in out
    assert len(split(bars)[0]) == 210
    cells = [Cell(p, k, 2.0 if (p, k) == (20, 2.0) else -0.5, 1.0, 40)
             for p in (10, 15, 20, 30, 40) for k in (1.0, 1.5, 2.0, 2.5)]
    assert is_fragile(cells, next(c for c in cells if c.period == 20 and c.k == 2.0))
