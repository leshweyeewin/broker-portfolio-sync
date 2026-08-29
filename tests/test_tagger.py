"""Tests for analytics.screening.tagger — strategy tagging engine."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from adapters.base import Broker, OptionAction, OptionTrade, OptionType, StockAction, StockTrade
from analytics.screening.tagger import (
    TAG_DAY_TRADE,
    TAG_EARNINGS_IV_CRUSH,
    TAG_MEDIUM_TERM,
    TAG_UNTAGGED,
    tag_option_trades,
    tag_stock_trades,
)


def _stock(ticker="AAPL", action="Buy", d="2025-06-15", fill_id="s1"):
    return StockTrade(
        date=date.fromisoformat(d),
        broker=Broker.TIGER,
        ticker=ticker,
        action=StockAction(action),
        qty=Decimal("10"),
        price=Decimal("150"),
        fee=Decimal("1"),
        currency="USD",
        dedup_key=f"Tiger:{fill_id}",
    )


def _option(underlying="AAPL", action="Sell", d="2025-06-15", fill_id="o1",
            strategy="", expiry="2025-07-18", strike="150"):
    return OptionTrade(
        date=date.fromisoformat(d),
        broker=Broker.TIGER,
        underlying=underlying,
        option_type=OptionType.PUT,
        strike=Decimal(strike),
        qty=Decimal("1"),
        expiry=date.fromisoformat(expiry),
        action=OptionAction(action),
        premium=Decimal("3.50"),
        fee=Decimal("0.65"),
        currency="USD",
        strategy=strategy,
        dedup_key=f"Tiger:{fill_id}",
    )


def _td(trade, status="Open"):
    return {"trade": trade, "status": status, "raw": []}


# --------------------------------------------------------------------------- #
# Stock tagging
# --------------------------------------------------------------------------- #

class TestStockTagging:
    def test_day_trade(self):
        """Buy and Sell on the same date → both tagged as Day Trade."""
        buy = _stock(action="Buy", d="2025-06-15", fill_id="b1")
        sell = _stock(action="Sell", d="2025-06-15", fill_id="s1")
        trades = [_td(buy), _td(sell, "Closed")]

        with patch("analytics.screening.tagger.is_near_earnings", return_value=False):
            tags = tag_stock_trades(trades)

        assert tags[buy.dedup_key] == TAG_DAY_TRADE
        assert tags[sell.dedup_key] == TAG_DAY_TRADE

    def test_medium_term(self):
        """Position held ≥ 2 days → medium-term."""
        buy = _stock(action="Buy", d="2025-06-10", fill_id="b1")
        sell = _stock(action="Sell", d="2025-06-15", fill_id="s1")
        trades = [_td(buy), _td(sell, "Closed")]

        with patch("analytics.screening.tagger.is_near_earnings", return_value=False):
            tags = tag_stock_trades(trades)

        assert tags[sell.dedup_key] == TAG_MEDIUM_TERM

    @patch("analytics.screening.tagger.is_near_earnings", return_value=True)
    def test_earnings_iv_crush(self, mock_earnings):
        """Trade near earnings date → earnings IV crush."""
        buy = _stock(action="Buy", d="2025-06-15", fill_id="b1")
        trades = [_td(buy)]

        tags = tag_stock_trades(trades)
        assert tags[buy.dedup_key] == TAG_EARNINGS_IV_CRUSH

    def test_earnings_overrides_day_trade(self):
        """Earnings tag takes priority over day trade."""
        buy = _stock(action="Buy", d="2025-06-15", fill_id="b1")
        sell = _stock(action="Sell", d="2025-06-15", fill_id="s1")
        trades = [_td(buy), _td(sell, "Closed")]

        with patch("analytics.screening.tagger.is_near_earnings", return_value=True):
            tags = tag_stock_trades(trades)

        assert tags[buy.dedup_key] == TAG_EARNINGS_IV_CRUSH
        assert tags[sell.dedup_key] == TAG_EARNINGS_IV_CRUSH

    def test_untagged_open_position(self):
        """An open buy with no matching sell stays untagged."""
        buy = _stock(action="Buy", d="2025-06-15", fill_id="b1")
        trades = [_td(buy)]

        with patch("analytics.screening.tagger.is_near_earnings", return_value=False):
            tags = tag_stock_trades(trades)

        assert tags[buy.dedup_key] == TAG_UNTAGGED


# --------------------------------------------------------------------------- #
# Option tagging
# --------------------------------------------------------------------------- #

class TestOptionTagging:
    def test_strategy_field_iv_crush(self):
        """Strategy field containing 'IV Crush' → tagged as earnings IV crush."""
        opt = _option(strategy="IV Crush Short Put", fill_id="o1")
        trades = [_td(opt)]

        with patch("analytics.screening.tagger.is_near_earnings", return_value=False):
            tags = tag_option_trades(trades)

        assert tags[opt.dedup_key] == TAG_EARNINGS_IV_CRUSH

    def test_option_day_trade(self):
        """Option opened and closed same day → day trade."""
        sell = _option(action="Sell", d="2025-06-15", fill_id="o1")
        buy = _option(action="Buy", d="2025-06-15", fill_id="o2")
        trades = [_td(sell), _td(buy, "Closed")]

        with patch("analytics.screening.tagger.is_near_earnings", return_value=False):
            tags = tag_option_trades(trades)

        assert tags[sell.dedup_key] == TAG_DAY_TRADE
        assert tags[buy.dedup_key] == TAG_DAY_TRADE

    def test_empty_trades(self):
        """No trades → empty tags."""
        assert tag_stock_trades([]) == {}
        assert tag_option_trades([]) == {}
