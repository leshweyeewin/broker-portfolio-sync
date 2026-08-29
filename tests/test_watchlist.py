"""Offline tests for the broker-derived earnings watchlist (pure extractor)."""

from decimal import Decimal

from adapters.base import AssetType, Broker, OptionType, Position
from analytics.earnings.watchlist import underlyings_from_positions


def _stock(broker, symbol, qty="10"):
    return Position(broker, AssetType.STOCK, symbol, Decimal(qty), Decimal("100"), "USD")


def _option(broker, underlying, qty="-1"):
    return Position(broker, AssetType.OPTION, underlying, Decimal(qty), Decimal("2"), "USD",
                    option_type=OptionType.PUT, strike=Decimal("90"))


def test_collects_moomoo_and_tiger_underlyings_deduped_and_sorted():
    positions = [
        _stock(Broker.MOOMOO, "NVDA"),
        _option(Broker.TIGER, "NVDA"),   # same underlying via an option leg → deduped
        _stock(Broker.TIGER, "AAPL"),
        _option(Broker.MOOMOO, "CRWD"),
    ]
    assert underlyings_from_positions(positions) == ["AAPL", "CRWD", "NVDA"]


def test_excludes_other_brokers_by_default():
    positions = [
        _stock(Broker.LONGBRIDGE, "TSLA"),   # not MooMoo/Tiger → excluded
        _stock(Broker.MOOMOO, "COST"),
    ]
    assert underlyings_from_positions(positions) == ["COST"]


def test_broker_filter_is_configurable():
    positions = [_stock(Broker.LONGBRIDGE, "TSLA"), _stock(Broker.MOOMOO, "COST")]
    assert underlyings_from_positions(positions, brokers=(Broker.LONGBRIDGE,)) == ["TSLA"]


def test_drops_zero_qty_and_non_us_symbols():
    positions = [
        _stock(Broker.MOOMOO, "NVDA", qty="0"),   # closed position
        _stock(Broker.TIGER, "700"),              # HK numeric ticker → not US equity
        _stock(Broker.TIGER, "BABA.US"),          # has a dot → filtered
        _stock(Broker.MOOMOO, "AAPL"),
    ]
    assert underlyings_from_positions(positions) == ["AAPL"]


def test_empty_positions_yields_empty_list():
    assert underlyings_from_positions([]) == []
