import sys
from unittest.mock import MagicMock

import pytest
import pandas as pd
from analytics.screening.swing import (
    theme_of,
    _rsi,
    _atr_pct,
    _classify,
    SwingSetup,
    format_swing_message,
    scan_swing_setups,
)

def test_theme_of():
    assert theme_of("NVDA") == "AI/Compute"
    assert theme_of("AAPL") == "MAG7"
    assert theme_of("UNKNOWN") == "Other"

def test_rsi_calculation():
    # Less than period returns None
    assert _rsi(pd.Series([1, 2, 3]), period=14) is None
    
    # 15 days of steady increase -> high RSI
    closes_up = pd.Series([10 + i for i in range(15)])
    rsi_up = _rsi(closes_up)
    assert rsi_up is not None
    assert rsi_up > 80

def test_atr_pct_calculation():
    assert _atr_pct(pd.DataFrame()) is None
    
    # Simple steady range
    data = []
    for i in range(15):
        data.append({"High": 12, "Low": 8, "Close": 10})
    df = pd.DataFrame(data)
    
    atr = _atr_pct(df)
    assert atr is not None
    # TR is always 4. ATR is 4. Price is 10. 4 / 10 = 40%
    assert atr == 40.0

def test_classify():
    # Base: Missing MAs
    assert _classify(100, None, 50, 200, 50, 5)[0] == "Base"
    
    # Uptrend vs Downtrend stack
    # Stack: 20 > 50 > 200
    assert _classify(110, 105, 100, 95, 50, 10)[0] == "Uptrend"
    assert _classify(90, 105, 100, 95, 50, 10)[0] == "Base" # Below MAs but up stack
    
    # Pullback buy: dipped to 20-DMA
    assert _classify(105, 105, 100, 95, 50, 10)[0] == "Pullback-buy"
    
    # Breakout: near 52w high, RSI > 60
    assert _classify(115, 105, 100, 95, 65, 2)[0] == "Breakout"
    
    # Overbought: RSI > 75
    assert _classify(120, 105, 100, 95, 80, 2)[0] == "Overbought"
    
    # Downtrend: 20 < 50
    assert _classify(90, 95, 100, 105, 40, 20)[0] == "Downtrend"

def test_format_swing_message():
    s1 = SwingSetup(ticker="AAPL", price=150, setup="Breakout", rsi14=65, atr_pct=5)
    s2 = SwingSetup(ticker="NVDA", price=500, setup="Pullback-buy", rsi14=55, atr_pct=6)
    s3 = SwingSetup(ticker="TSLA", price=200, setup="Downtrend") # Not actionable
    
    msg = format_swing_message([s1, s2, s3])

    assert "AAPL" in msg
    assert "NVDA" in msg
    assert "TSLA" not in msg # Downtrend is not actionable
    assert "🚀" in msg # Breakout
    assert "🎯" in msg # Pullback
    # ATR-based bracket appears on its own line.
    assert "TP $" in msg
    assert "SL $" in msg


def test_swing_targets_from_atr():
    # AAPL $150, ATR 5% -> ATR = $7.50. Stop = 150 - 1.5*7.5 = 138.75;
    # target = 150 + 3*7.5 = 172.50 (2:1 reward:risk).
    s = SwingSetup(ticker="AAPL", price=150, setup="Breakout", atr_pct=5)
    assert s.stop_loss == 138.75
    assert s.take_profit == 172.50


def test_swing_targets_none_without_atr():
    s = SwingSetup(ticker="XYZ", price=150, setup="Breakout")  # atr_pct None
    assert s.stop_loss is None
    assert s.take_profit is None


def test_scan_swing_setups_handles_multiindex_columns(monkeypatch):
    # Regression: yf.download(group_by="ticker") returns MultiIndex columns
    # like ('MU','Close') even for a single ticker. The scanner must extract the
    # ticker sub-frame, not treat the whole thing as flat (which returned No-data).
    n = 60
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    prices = [100.0 + i for i in range(n)]  # steady uptrend
    frame = pd.DataFrame(
        {
            ("MU", "Open"): prices,
            ("MU", "High"): [p + 1 for p in prices],
            ("MU", "Low"): [p - 1 for p in prices],
            ("MU", "Close"): prices,
            ("MU", "Volume"): [1_000_000] * n,
        },
        index=idx,
    )
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)

    fake_yf = MagicMock()
    fake_yf.download.return_value = frame
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    monkeypatch.setattr("analytics.screening.swing.get_earnings_dates", lambda t: [])

    setups = scan_swing_setups(["MU"])
    assert len(setups) == 1
    assert setups[0].ticker == "MU"
    assert setups[0].setup != "No-data"
    assert setups[0].price == pytest.approx(prices[-1], abs=0.01)
