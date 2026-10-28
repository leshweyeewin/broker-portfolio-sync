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
