from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from analytics.options.market_context import build_market_context
from analytics.options.option_chain import OptionChainSnapshot, OptionContract, OptionQuote


def _snapshot():
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    expiry = date(2026, 9, 18)
    call = OptionQuote(OptionContract("AAPL-C", "AAPL", expiry, 200, "call"), now, "broker_live", "5", "5.20")
    put = OptionQuote(OptionContract("AAPL-P", "AAPL", expiry, 200, "put"), now, "broker_live", "4.80", "5")
    return OptionChainSnapshot("AAPL", 200, now, "broker_live", (call, put))


def test_context_derives_atm_straddle_expected_move():
    context = build_market_context("aapl", snapshot=_snapshot(), iv_rank="65")
    move = context.expected_moves[0]
    assert context.price == Decimal("200")
    assert move.straddle_price == Decimal("10")
    assert move.move_pct == Decimal("5.00")
    assert move.lower == Decimal("190")
    assert move.upper == Decimal("210")
    assert context.snapshot_id
    assert "technical context unavailable" in context.warnings


def test_context_keeps_missing_data_explicit_and_checks_ticker_match():
    context = build_market_context("AAPL")
    assert context.expected_moves == ()
    assert "option-chain snapshot unavailable" in context.warnings
    assert "IV rank unavailable; do not infer it from current IV" in context.warnings
    with pytest.raises(ValueError, match="snapshot underlying"):
        build_market_context("MSFT", snapshot=_snapshot())


def test_context_rejects_out_of_range_iv_rank():
    with pytest.raises(ValueError, match="iv_rank"):
        build_market_context("AAPL", iv_rank="101")
