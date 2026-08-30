"""Offline tests for the pure credit-spread / iron-condor builder (Slice 3)."""

from datetime import date, datetime, timezone
from decimal import Decimal

from analytics.options.option_chain import OptionContract, OptionQuote, OptionChainSnapshot
from analytics.options.strategies import (
    SpreadFilters,
    build_call_credit_spreads,
    build_iron_condors,
    build_put_credit_spreads,
    rsi_signal_context,
)

TODAY = date(2026, 1, 1)
EXPIRY = date(2026, 2, 5)  # 35 DTE from TODAY
AS_OF = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)


def _quote(strike, right, bid, ask, delta, *, oi=500, vol=120):
    contract = OptionContract(f"XYZ{strike}{right[0].upper()}", "XYZ", EXPIRY, strike, right)
    return OptionQuote(
        contract, AS_OF, "broker_live", bid=bid, ask=ask, last=None,
        volume=vol, open_interest=oi, implied_volatility="0.35", delta=delta,
    )


def _snapshot(quotes, price="100"):
    return OptionChainSnapshot("XYZ", price, AS_OF, "broker_live", tuple(quotes))


def _base_chain():
    # Puts: 95 short-able (|delta| 0.25), 90 protective; a 90 short is off-band.
    # Calls: 105 short-able (delta 0.25), 110 protective.
    return _snapshot([
        _quote(Decimal("95"), "put", "2.0", "2.2", "-0.25"),
        _quote(Decimal("90"), "put", "1.0", "1.1", "-0.10"),
        _quote(Decimal("105"), "call", "2.0", "2.2", "0.25"),
        _quote(Decimal("110"), "call", "1.0", "1.1", "0.10"),
    ])


def test_put_credit_spread_happy_path_has_defined_risk_and_metrics():
    scan = build_put_credit_spreads(_base_chain(), EXPIRY, today=TODAY)
    assert len(scan.candidates) == 1
    c = scan.candidates[0]
    assert c.strategy == "put_credit"
    assert c.short_strikes == (Decimal("95"),)
    assert c.width == Decimal("5")
    assert c.dte == 35
    assert c.credit_per_share == Decimal("1.05")  # 2.1 mid short - 1.05 mid long
    assert c.net_credit == Decimal("105")
    assert c.max_loss == Decimal("395")
    assert c.max_profit == Decimal("105")
    assert c.return_on_risk == Decimal("105") / Decimal("395")
    # The off-band 90 short is reported as a rejection, not silently dropped.
    assert any(r.short_strike == Decimal("90") for r in scan.rejections)


def test_call_credit_spread_happy_path():
    scan = build_call_credit_spreads(_base_chain(), EXPIRY, today=TODAY)
    assert len(scan.candidates) == 1
    c = scan.candidates[0]
    assert c.strategy == "call_credit"
    assert c.short_strikes == (Decimal("105"),)
    assert c.max_loss == Decimal("395")


def test_iron_condor_combines_both_wings():
    scan = build_iron_condors(_base_chain(), EXPIRY, today=TODAY)
    assert len(scan.candidates) == 1
    c = scan.candidates[0]
    assert c.strategy == "iron_condor"
    assert set(c.short_strikes) == {Decimal("95"), Decimal("105")}
    assert len(c.legs) == 4
    # Symmetric 1.05 credit per wing => 2.10 total; loss = 5 - 2.10 = 2.90/share.
    assert c.net_credit == Decimal("210")
    assert c.max_loss == Decimal("290")


def test_iron_condor_needs_both_wings():
    # Only puts present -> no call wing -> no condor, explicit rejection.
    chain = _snapshot([
        _quote(Decimal("95"), "put", "2.0", "2.2", "-0.25"),
        _quote(Decimal("90"), "put", "1.0", "1.1", "-0.22"),
    ])
    scan = build_iron_condors(chain, EXPIRY, today=TODAY)
    assert scan.candidates == ()
    assert any("wing" in r.detail for r in scan.rejections)


def test_dte_outside_window_is_rejected_with_no_candidates():
    scan = build_put_credit_spreads(_base_chain(), EXPIRY, today=date(2026, 1, 30))  # 6 DTE
    assert scan.candidates == ()
    assert any("DTE" in r.detail for r in scan.rejections)


def test_off_band_delta_is_rejected():
    chain = _snapshot([
        _quote(Decimal("95"), "put", "2.0", "2.2", "-0.10"),  # too far OTM
        _quote(Decimal("90"), "put", "1.0", "1.1", "-0.05"),
    ])
    scan = build_put_credit_spreads(chain, EXPIRY, today=TODAY)
    assert scan.candidates == ()
    assert any("delta" in r.detail for r in scan.rejections)


def test_missing_delta_fails_safe_rather_than_guessing():
    chain = _snapshot([
        _quote(Decimal("95"), "put", "2.0", "2.2", None),
        _quote(Decimal("90"), "put", "1.0", "1.1", None),
    ])
    scan = build_put_credit_spreads(chain, EXPIRY, today=TODAY)
    assert scan.candidates == ()
    assert any("delta unavailable" in r.detail for r in scan.rejections)


def test_low_liquidity_short_leg_is_rejected():
    # Very wide bid-ask on the short blows the spread limit -> not tradable.
    chain = _snapshot([
        _quote(Decimal("95"), "put", "1.0", "3.0", "-0.25"),
        _quote(Decimal("90"), "put", "1.0", "1.1", "-0.25"),
    ])
    scan = build_put_credit_spreads(chain, EXPIRY, today=TODAY)
    assert scan.candidates == ()
    assert any("not tradable" in r.detail for r in scan.rejections)


def test_no_long_within_width_is_rejected_geometry():
    # Short 95, protective put only 20 points away -> outside max_width.
    chain = _snapshot([
        _quote(Decimal("95"), "put", "2.0", "2.2", "-0.25"),
        _quote(Decimal("75"), "put", "0.5", "0.6", "-0.10"),
    ])
    scan = build_put_credit_spreads(chain, EXPIRY, today=TODAY)
    assert scan.candidates == ()
    assert any("within width" in r.detail and r.short_strike == Decimal("95")
               for r in scan.rejections)


def test_strike_inside_expected_move_warns_but_keeps_candidate():
    # EM 8 => lower bound 92; short 95 sits INSIDE the band -> warning.
    scan = build_put_credit_spreads(_base_chain(), EXPIRY, today=TODAY, expected_move="8")
    c = scan.candidates[0]
    assert c.components.expected_move_buffer < 0
    assert any("expected-move buffer" in w for w in c.warnings)


def test_strike_beyond_expected_move_has_positive_buffer_no_warning():
    # EM 3 => lower bound 97; short 95 sits OUTSIDE (below) the band.
    scan = build_put_credit_spreads(_base_chain(), EXPIRY, today=TODAY, expected_move="3")
    c = scan.candidates[0]
    assert c.components.expected_move_buffer > 0
    assert not any("expected-move buffer" in w for w in c.warnings)


def test_missing_expected_move_is_reported_not_guessed():
    scan = build_put_credit_spreads(_base_chain(), EXPIRY, today=TODAY)
    assert scan.candidates[0].components.expected_move_buffer is None
    assert any("expected-move buffer not evaluated" in w for w in scan.warnings)


def test_earnings_inside_window_warns_and_penalizes_score():
    inside = build_put_credit_spreads(_base_chain(), EXPIRY, today=TODAY,
                                      earnings_date=date(2026, 1, 20))
    outside = build_put_credit_spreads(_base_chain(), EXPIRY, today=TODAY,
                                       earnings_date=date(2026, 3, 1))
    ci, co = inside.candidates[0], outside.candidates[0]
    assert ci.components.event_risk_penalty > 0
    assert co.components.event_risk_penalty == 0
    assert any("earnings" in w for w in ci.warnings)
    assert ci.score < co.score


def test_block_earnings_rejects_the_whole_expiry():
    scan = build_put_credit_spreads(
        _base_chain(), EXPIRY, today=TODAY, earnings_date=date(2026, 1, 20),
        filters=SpreadFilters(block_earnings=True))
    assert scan.candidates == ()
    assert any("blocked" in r.detail for r in scan.rejections)


def test_iv_rank_supplied_populates_component_else_none():
    with_iv = build_put_credit_spreads(_base_chain(), EXPIRY, today=TODAY, iv_rank=80)
    without = build_put_credit_spreads(_base_chain(), EXPIRY, today=TODAY)
    assert with_iv.candidates[0].components.iv_context == Decimal("0.8")
    assert without.candidates[0].components.iv_context is None


def test_min_return_on_risk_filter_rejects_thin_credit():
    # Demand an unrealistic 5:1 return-on-risk -> the 0.27 R/R candidate is out.
    scan = build_put_credit_spreads(
        _base_chain(), EXPIRY, today=TODAY,
        filters=SpreadFilters(min_return_on_risk=Decimal("5")))
    assert scan.candidates == ()
    assert any("return-on-risk" in r.detail for r in scan.rejections)


def test_rsi_signal_context_is_context_not_advice():
    for strat in ("put_credit", "call_credit", "iron_condor"):
        for rsi in (None, 25.0, 55.0, 80.0):
            msg = rsi_signal_context(rsi, strat)  # type: ignore[arg-type]
            assert "buy" not in msg.lower()
            assert "sell" not in msg.lower()
    assert "oversold" in rsi_signal_context(25.0, "put_credit").lower()
    assert "overbought" in rsi_signal_context(80.0, "call_credit").lower()
    assert "neutral" in rsi_signal_context(55.0, "iron_condor").lower()


def test_min_credit_is_set_via_filters_not_a_builder_kwarg():
    # Regression guard for the iv_crush wiring: the credit floor is a SpreadFilters
    # field, passed through ``filters=`` — the builders take no ``min_credit`` kwarg.
    kept = build_put_credit_spreads(
        _base_chain(), EXPIRY, today=TODAY,
        filters=SpreadFilters(min_credit=Decimal("0.20")))
    assert len(kept.candidates) == 1  # 1.05/sh credit clears a 0.20 floor

    dropped = build_put_credit_spreads(
        _base_chain(), EXPIRY, today=TODAY,
        filters=SpreadFilters(min_credit=Decimal("2.0")))
    assert dropped.candidates == ()
    assert any("not above min" in r.detail for r in dropped.rejections)

    import pytest
    with pytest.raises(TypeError):
        build_put_credit_spreads(_base_chain(), EXPIRY, today=TODAY, min_credit=Decimal("0.20"))


def test_spread_filters_short_term_window():
    from analytics.options.strategies import SpreadFiltersShortTerm
    sf = SpreadFiltersShortTerm()
    assert (sf.min_dte, sf.max_dte) == (7, 14)
