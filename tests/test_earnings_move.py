"""Offline unit tests for ``analytics.earnings.earnings_move``.

Cover the pure measurement core (``measure_earnings_moves``), the Step-4 edge
verdict (``implied_vs_historical``), and the study aggregation — all with
synthetic price series, no yfinance / network.
"""

from __future__ import annotations

from datetime import date, timedelta

from analytics.earnings.earnings_move import (
    EarningsMove,
    EarningsMoveStudy,
    implied_vs_historical,
    measure_earnings_moves,
)


def _series(start: date, closes: list[float]) -> list[tuple[date, float]]:
    """Build a daily (date, close) series starting at ``start`` (weekdays-ish)."""
    return [(start + timedelta(days=i), c) for i, c in enumerate(closes)]


# --------------------------------------------------------------------------- #
# measure_earnings_moves — reaction detection
# --------------------------------------------------------------------------- #

def test_reaction_picks_larger_of_event_day_and_next():
    # Earnings on index 5. Event day (idx5) +2%, next day (idx6) -10% (bigger).
    closes = [100, 100, 100, 100, 100, 102, 91.8, 92, 92, 92]
    start = date(2026, 1, 1)
    ed = start + timedelta(days=5)
    moves = measure_earnings_moves(_series(start, closes), [ed], pre_window=3, post_window=2)
    assert len(moves) == 1
    m = moves[0]
    # -10% reaction on the day after the earnings date.
    assert m.reaction_day == start + timedelta(days=6)
    assert m.reaction_pct == -10.0


def test_reaction_before_open_report_on_event_day():
    # Big move on the event day itself (before-open report).
    closes = [50, 50, 50, 50, 55, 55, 55]
    start = date(2026, 3, 2)
    ed = start + timedelta(days=4)
    moves = measure_earnings_moves(_series(start, closes), [ed], pre_window=2, post_window=1)
    assert moves[0].reaction_day == ed
    assert moves[0].reaction_pct == 10.0


def test_pre_and_post_drift_measured():
    # idx: 0..8 ; earnings at idx4; reaction on idx4 (+10%).
    closes = [90, 95, 100, 100, 110, 110, 110, 121, 121]
    start = date(2026, 6, 1)
    ed = start + timedelta(days=4)
    moves = measure_earnings_moves(_series(start, closes), [ed], pre_window=2, post_window=2)
    m = moves[0]
    # pre: close[idx3]=100 vs close[idx1]=95 -> +5.26%
    assert m.pre_pct == 5.26
    # post: close[idx6]=110 vs close[idx4]=110 -> 0.0 (reaction day is idx4)
    assert m.post_pct == 0.0


def test_event_skipped_when_no_prior_day():
    # Earnings on/before the first bar -> no prev close -> skipped.
    closes = [100, 101, 102, 103]
    start = date(2026, 1, 1)
    moves = measure_earnings_moves(_series(start, closes), [start])
    assert moves == []


def test_pre_post_none_when_series_too_short():
    closes = [100, 100, 105, 105]  # earnings at idx2, no room for pre/post windows
    start = date(2026, 1, 1)
    ed = start + timedelta(days=2)
    moves = measure_earnings_moves(_series(start, closes), [ed], pre_window=5, post_window=5)
    assert len(moves) == 1
    assert moves[0].pre_pct is None
    assert moves[0].post_pct is None


def test_multiple_earnings_dates_sorted_and_deduped():
    closes = [100] * 20
    closes[5] = 110   # reaction at idx5
    closes[12] = 90   # reaction at idx12
    start = date(2026, 1, 1)
    e1 = start + timedelta(days=5)
    e2 = start + timedelta(days=12)
    moves = measure_earnings_moves(_series(start, closes), [e2, e1, e1], pre_window=2, post_window=2)
    assert [m.earnings_date for m in moves] == [e1, e2]


# --------------------------------------------------------------------------- #
# EarningsMoveStudy aggregation
# --------------------------------------------------------------------------- #

def _study(reactions: list[float]) -> EarningsMoveStudy:
    evs = [
        EarningsMove(
            earnings_date=date(2026, 1, 1) + timedelta(days=90 * i),
            reaction_day=date(2026, 1, 2) + timedelta(days=90 * i),
            reaction_pct=r,
            pre_pct=1.0,
            post_pct=-2.0,
        )
        for i, r in enumerate(reactions)
    ]
    return EarningsMoveStudy(ticker="TST", events=evs)


def test_study_avg_abs_reaction_uses_magnitude():
    s = _study([10.0, -10.0, 4.0, -4.0])
    assert s.avg_abs_reaction == 7.0  # (10+10+4+4)/4
    assert s.n == 4


def test_study_bull_bear_and_consistency():
    s = _study([5.0, 6.0, 7.0, -1.0])
    assert s.bull_count == 3
    assert s.bear_count == 1
    assert s.directional_consistency == 0.75


def test_study_avg_abs_pre_post():
    s = _study([1.0, 2.0])
    assert s.avg_abs_pre == 1.0
    assert s.avg_abs_post == 2.0


def test_study_empty_is_safe():
    s = EarningsMoveStudy(ticker="X", events=[])
    assert s.n == 0
    assert s.avg_abs_reaction is None
    assert s.directional_consistency is None


# --------------------------------------------------------------------------- #
# implied_vs_historical (Step-4 edge verdict)
# --------------------------------------------------------------------------- #

def test_implied_rich_when_above_history():
    # CCL example from the deck: implied 2.53 vs hist 1.77 -> rich.
    assert implied_vs_historical(2.53, 1.77) == "RICH"


def test_implied_cheap_when_below_history():
    # GME example: implied 1.61 << hist 4.65 -> cheap.
    assert implied_vs_historical(1.61, 4.65) == "CHEAP"


def test_implied_fair_within_band():
    assert implied_vs_historical(10.3, 10.0) == "FAIR"


def test_implied_none_on_missing_inputs():
    assert implied_vs_historical(None, 5.0) is None
    assert implied_vs_historical(5.0, None) is None
    assert implied_vs_historical(5.0, 0.0) is None
