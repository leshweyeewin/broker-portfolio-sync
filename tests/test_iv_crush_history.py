"""Offline unit tests for ``analytics.earnings.iv_crush_history`` — Steps 2 & 3.

Feed the pure ``measure_iv_crush`` synthetic IV histories + earnings dates; no
snapshot file, earnings API, or network involved.
"""

from __future__ import annotations

from datetime import date

import pytest

from analytics.earnings.iv_crush_history import CrushStudy, measure_iv_crush


def test_consistent_double_digit_crush():
    # IV 0.60 the day before each earnings, 0.40 the day after -> 33.3% crush, always.
    hist = {
        "2026-03-19": 0.60, "2026-03-21": 0.40,
        "2026-06-18": 0.60, "2026-06-20": 0.40,
    }
    study = measure_iv_crush(hist, [date(2026, 3, 20), date(2026, 6, 19)])
    assert study.n == 2
    assert study.consistency == 1.0
    assert study.avg_crush_pct == pytest.approx(33.33, abs=0.1)


def test_mixed_direction_halves_consistency():
    hist = {
        "2026-03-19": 0.60, "2026-03-21": 0.40,   # IV fell (+33%)
        "2026-06-18": 0.40, "2026-06-20": 0.60,   # IV rose (-50%)
    }
    study = measure_iv_crush(hist, [date(2026, 3, 20), date(2026, 6, 19)])
    assert study.n == 2
    assert study.consistency == 0.5


def test_event_without_post_snapshot_is_excluded():
    # No snapshot within the 3-day post window for the second event.
    hist = {
        "2026-03-19": 0.60, "2026-03-21": 0.40,
        "2026-06-18": 0.60, "2026-06-30": 0.40,   # 11 days later -> out of window
    }
    study = measure_iv_crush(hist, [date(2026, 3, 20), date(2026, 6, 19)])
    assert study.n == 1
    assert [ed for ed, _ in study.events] == [date(2026, 3, 20)]


def test_event_without_pre_snapshot_is_excluded():
    hist = {"2026-03-21": 0.40}  # only a post snapshot, no pre
    study = measure_iv_crush(hist, [date(2026, 3, 20)])
    assert study.n == 0


def test_empty_history_is_all_none():
    study = measure_iv_crush({}, [date(2026, 3, 20)])
    assert study == CrushStudy(n=0, consistency=None, avg_crush_pct=None, events=[])


def test_no_earnings_dates_is_all_none():
    study = measure_iv_crush({"2026-03-19": 0.6}, [])
    assert study.n == 0 and study.avg_crush_pct is None
