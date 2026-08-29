from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from analytics.options.option_chain import OptionChainSnapshot, OptionContract, OptionQuote, SnapshotStore, evaluate_quote_quality


def _quote(*, right="call", bid="2", ask="2.20", last="2.10", oi=1000):
    contract = OptionContract("AAPL260918C00200000", "AAPL", date(2026, 9, 18), 200, right)
    return OptionQuote(contract, datetime(2026, 8, 29, tzinfo=timezone.utc), "broker_live", bid, ask, last, 12, oi, "0.3")


def test_strict_midpoint_and_liquidity_quality():
    quality = evaluate_quote_quality(_quote(), min_open_interest=500, max_spread_pct="0.20")
    assert quality.price == Decimal("2.10")
    assert quality.spread == Decimal("0.20")
    assert quality.volume_open_interest_ratio == Decimal("0.012")
    assert quality.tradable
    assert quality.warnings == ()


def test_invalid_bid_ask_uses_labelled_last_and_is_not_tradable():
    quote = _quote(bid="2.30", ask="2.20", last="2.25")
    quality = evaluate_quote_quality(quote)
    assert quote.midpoint is None
    assert quality.price == Decimal("2.25")
    assert not quality.tradable
    assert "no valid two-sided quote; using last trade only" in quality.warnings


def test_snapshot_is_deterministic_and_rejects_wrong_underlying():
    quote = _quote()
    a = OptionChainSnapshot("AAPL", "201", quote.as_of, "broker_live", (quote,))
    b = OptionChainSnapshot("AAPL", "201", quote.as_of, "broker_live", (quote,))
    assert a.snapshot_id == b.snapshot_id
    other = OptionQuote(OptionContract("MSFT260918C00400000", "MSFT", date(2026, 9, 18), 400, "call"), quote.as_of, "broker_live")
    with pytest.raises(ValueError, match="snapshot underlying"):
        OptionChainSnapshot("AAPL", 201, quote.as_of, "broker_live", (other,))


def test_quality_flags_wide_or_illiquid_quote():
    quality = evaluate_quote_quality(_quote(bid="1", ask="2", oi=10), min_open_interest=100, max_spread_pct="0.1")
    assert not quality.tradable
    assert "bid-ask spread exceeds configured limit" in quality.warnings
    assert "open interest below configured minimum" in quality.warnings


def test_quote_can_be_marked_stale_and_snapshot_round_trips(tmp_path):
    quote = _quote()
    quality = evaluate_quote_quality(quote, now=datetime(2026, 8, 30, tzinfo=timezone.utc), max_age_seconds=60)
    assert quality.stale
    assert not quality.tradable
    snapshot = OptionChainSnapshot("AAPL", 201, quote.as_of, "broker_live", (quote,))
    store = SnapshotStore(tmp_path / "snapshots.json")
    store.save(snapshot)
    restored = store.get(snapshot.snapshot_id)
    assert restored is not None
    assert restored.quotes[0].contract.strike == Decimal("200")
