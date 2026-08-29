import pytest
import sys
from unittest.mock import patch, MagicMock

sys.modules["yfinance"] = MagicMock()

from datetime import date
from datetime import date
from analytics.options.directional_builder import main, _get_snapshot

def test_directional_builder_main_no_tickers(capsys):
    assert main([]) == 1
    out, err = capsys.readouterr()
    assert "No tickers provided" in out

@patch("analytics.options.directional_builder._build_quote_client")
@patch("analytics.options.directional_builder._get_snapshot")
def test_directional_builder_main_with_tickers(mock_get_snapshot, mock_client, capsys):
    mock_snap = MagicMock()
    mock_snap.quotes = []
    mock_get_snapshot.return_value = mock_snap
    
    assert main(["AAPL"]) == 0
    out, err = capsys.readouterr()
    assert "Scanning AAPL" in out

@patch("yfinance.Ticker")
@patch("analytics.options.directional_builder.fetch_option_chain")
def test_get_snapshot(mock_fetch_chain, mock_ticker):
    mock_tk = MagicMock()
    mock_tk.fast_info.last_price = 150.0
    mock_tk.options = ["2026-02-05", "2028-02-05"]
    mock_ticker.return_value = mock_tk
    
    mock_fetch_chain.return_value = [
        {"right": "call", "strike": 150, "bid": 5, "ask": 6, "open_interest": 100, "delta": 0.5}
    ]
    
    snap = _get_snapshot(MagicMock(), "AAPL")
    assert snap is not None
    assert snap.underlying == "AAPL"
    assert snap.underlying_price == 150.0
    assert len(snap.quotes) == 2
