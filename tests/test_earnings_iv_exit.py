"""Tests for alerting/earnings_iv_exit.py — post-earnings IV-crush exit reminder.

Offline: builds Position objects and monkeypatches the earnings-date lookup; no
adapters or network involved.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from adapters.base import AssetType, Broker, OptionType, Position
from alerting import earnings_iv_exit as mod
from alerting.earnings_iv_exit import (
    evaluate_earnings_exits,
    format_message,
    recent_earnings,
)

TODAY = date(2026, 8, 29)


def _opt(symbol="NVDA", qty=-2, *, option_type=OptionType.CALL):
    return Position(
        broker=Broker.MOOMOO, asset_type=AssetType.OPTION, symbol=symbol,
        qty=qty, avg_cost=Decimal("3"), currency="USD", market_price=Decimal("1.5"),
        option_type=option_type, strike=Decimal("180"), expiry=date(2026, 9, 18),
    )


def _stock(symbol="NVDA", qty=100):
    return Position(
        broker=Broker.MOOMOO, asset_type=AssetType.STOCK, symbol=symbol,
        qty=qty, avg_cost=Decimal("150"), currency="USD", market_price=Decimal("155"),
    )


@pytest.fixture
def earnings_map(monkeypatch):
    """Patch get_earnings_dates with a per-symbol lookup table."""
    table: dict[str, list[date]] = {}
    monkeypatch.setattr(mod, "get_earnings_dates", lambda s: table.get(s.upper(), []))
    return table


# --------------------------------------------------------------------------- #
# recent_earnings
# --------------------------------------------------------------------------- #

def test_recent_earnings_within_window(earnings_map):
    earnings_map["NVDA"] = [TODAY - timedelta(days=1)]
    assert recent_earnings("NVDA", TODAY) == TODAY - timedelta(days=1)


def test_recent_earnings_today(earnings_map):
    earnings_map["NVDA"] = [TODAY]
    assert recent_earnings("NVDA", TODAY) == TODAY


def test_recent_earnings_outside_window_is_none(earnings_map):
    earnings_map["NVDA"] = [TODAY - timedelta(days=5)]
    assert recent_earnings("NVDA", TODAY) is None


def test_recent_earnings_future_is_none(earnings_map):
    earnings_map["NVDA"] = [TODAY + timedelta(days=1)]
    assert recent_earnings("NVDA", TODAY) is None


def test_recent_earnings_picks_most_recent(earnings_map):
    earnings_map["NVDA"] = [TODAY - timedelta(days=2), TODAY - timedelta(days=1)]
    assert recent_earnings("NVDA", TODAY) == TODAY - timedelta(days=1)


def test_recent_earnings_respects_custom_lookback(earnings_map):
    earnings_map["NVDA"] = [TODAY - timedelta(days=4)]
    assert recent_earnings("NVDA", TODAY, lookback_days=2) is None
    assert recent_earnings("NVDA", TODAY, lookback_days=5) == TODAY - timedelta(days=4)


# --------------------------------------------------------------------------- #
# evaluate_earnings_exits
# --------------------------------------------------------------------------- #

def test_short_option_on_reported_stock_fires(earnings_map):
    earnings_map["NVDA"] = [TODAY - timedelta(days=1)]
    sigs = evaluate_earnings_exits([_opt(symbol="NVDA", qty=-2)], today=TODAY)
    assert len(sigs) == 1
    assert sigs[0].symbol == "NVDA"
    assert sigs[0].side == "short"
    assert sigs[0].days_since == 1


def test_long_option_also_fires(earnings_map):
    earnings_map["CRM"] = [TODAY]
    sigs = evaluate_earnings_exits([_opt(symbol="CRM", qty=3)], today=TODAY)
    assert len(sigs) == 1
    assert sigs[0].side == "long"


def test_stock_position_skipped(earnings_map):
    earnings_map["NVDA"] = [TODAY]
    assert evaluate_earnings_exits([_stock(symbol="NVDA")], today=TODAY) == []


def test_closed_option_skipped(earnings_map):
    earnings_map["NVDA"] = [TODAY]
    assert evaluate_earnings_exits([_opt(symbol="NVDA", qty=0)], today=TODAY) == []


def test_option_without_recent_earnings_skipped(earnings_map):
    earnings_map["NVDA"] = [TODAY - timedelta(days=30)]
    assert evaluate_earnings_exits([_opt(symbol="NVDA")], today=TODAY) == []


def test_sorted_by_days_since_then_symbol(earnings_map):
    earnings_map["NVDA"] = [TODAY - timedelta(days=2)]
    earnings_map["CRM"] = [TODAY]
    earnings_map["ADBE"] = [TODAY]
    sigs = evaluate_earnings_exits(
        [_opt(symbol="NVDA"), _opt(symbol="CRM"), _opt(symbol="ADBE")], today=TODAY)
    assert [s.symbol for s in sigs] == ["ADBE", "CRM", "NVDA"]


def test_format_message_lists_all(earnings_map):
    earnings_map["NKE"] = [TODAY - timedelta(days=1)]
    sigs = evaluate_earnings_exits([_opt(symbol="NKE", option_type=OptionType.PUT)], today=TODAY)
    msg = format_message(sigs)
    assert "NKE" in msg
    assert "Put" in msg
    assert "IV has crushed" in msg
