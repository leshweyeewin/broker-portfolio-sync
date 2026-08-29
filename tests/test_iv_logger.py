import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from analytics.earnings.iv_logger import fetch_atm_iv, log_iv_snapshots

def test_fetch_atm_iv_success():
    with patch("yfinance.Ticker") as mock_ticker:
        mock_tk = MagicMock()
        mock_ticker.return_value = mock_tk
        
        mock_tk.fast_info = MagicMock(last_price=100.0)
        mock_tk.options = ["2026-09-01", "2026-10-01"]
        
        mock_chain = MagicMock()
        mock_calls = MagicMock()
        mock_calls.empty = False
        
        import pandas as pd
        # Create a mock DataFrame with strike and impliedVolatility
        df = pd.DataFrame({
            "strike": [90.0, 100.0, 110.0],
            "impliedVolatility": [0.5, 0.4, 0.6]
        })
        mock_chain.calls = df
        mock_tk.option_chain.return_value = mock_chain
        
        iv = fetch_atm_iv("AAPL")
        assert iv == 0.4

def test_log_iv_snapshots(tmp_path):
    with patch("analytics.earnings.iv_logger.fetch_atm_iv") as mock_fetch, \
         patch("analytics.earnings.iv_logger.HISTORY_FILE", tmp_path / "iv_history.json"):
         
        mock_fetch.side_effect = lambda t: 0.35 if t == "AAPL" else 0.45
        
        log_iv_snapshots(["AAPL", "NVDA"])
        
        history_file = tmp_path / "iv_history.json"
        assert history_file.exists()
        
        with open(history_file) as f:
            data = json.load(f)
            
        assert "AAPL" in data
        assert "NVDA" in data
        # Check that today's date was logged
        from datetime import date
        today = date.today().isoformat()
        assert data["AAPL"][today] == 0.35
        assert data["NVDA"][today] == 0.45
