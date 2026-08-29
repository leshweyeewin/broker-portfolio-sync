from decimal import Decimal
from analytics.screening.screener import (
    ScreenerFilter,
    ScreenerResult,
    screen_options,
    format_screener_message,
    _extract_underlying
)

def test_extract_underlying():
    assert _extract_underlying("AAPL 240119C00190000") == "AAPL"
    assert _extract_underlying("AAPL240119C00190000") == "AAPL"
    assert _extract_underlying("NVDA") == "NVDA"

def test_screen_options_happy_path():
    chain = [
        {
            "identifier": "AAPL 240119C00190000",
            "strike": 150.0,
            "bid": 2.0,
            "ask": 2.1,
            "delta": 0.35, # Good delta
            "iv": 0.5,
            "ivp": 80, # Good IVP
            "oi": 1000, # Good OI
            "right": "CALL",
            "expiry": "20240119"
        },
        {
            "identifier": "AAPL 240119C00180000",
            "strike": 180.0,
            "bid": 1.0,
            "ask": 1.5, # Bad spread (0.5 > 0.1)
            "delta": 0.35,
            "iv": 0.5,
            "ivp": 80,
            "oi": 1000,
            "right": "CALL",
            "expiry": "20240119"
        },
        {
            "identifier": "AAPL 240119P00150000",
            "strike": 150.0,
            "bid": 1.0,
            "ask": 1.05,
            "delta": -0.15, # Bad delta (0.15 < 0.30)
            "iv": 0.5,
            "ivp": 80,
            "oi": 1000,
            "right": "PUT",
            "expiry": "20240119"
        }
    ]
    
    results = screen_options(chain)
    assert len(results) == 1
    assert results[0].symbol == "AAPL"
    assert results[0].strike == Decimal("150")
    assert results[0].option_type == "Call"
    assert results[0].spread == Decimal("0.1")
    assert results[0].mid_price == Decimal("2.05")

def test_format_screener_message():
    res = ScreenerResult(
        symbol="AAPL", expiry="20240119", option_type="Call",
        strike=Decimal("150"), bid=Decimal("2.0"), ask=Decimal("2.1"),
        spread=Decimal("0.1"), delta=0.35, iv=0.5, ivp=80, open_interest=1000,
        volume=10, mid_price=Decimal("2.05")
    )
    msg = format_screener_message([res])
    assert "AAPL" in msg
    assert "Short Call" in msg
    assert "🔴" in msg # Short call = bearish
    
    msg_empty = format_screener_message([])
    assert "No option setups matched" in msg_empty
