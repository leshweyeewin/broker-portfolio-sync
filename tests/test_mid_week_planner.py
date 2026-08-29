from datetime import date
from analytics.options.mid_week_planner import get_dte, main
import sys
from io import StringIO

def test_get_dte(monkeypatch):
    # Mock date.today() to return a fixed date
    class MockDate(date):
        @classmethod
        def today(cls):
            return cls(2024, 1, 1)
            
    monkeypatch.setattr("analytics.options.mid_week_planner.date", MockDate)
    
    assert get_dte("2024-01-05") == 4
    assert get_dte("2024-01-01") == 0
    assert get_dte("2024-01-06") == 5
    assert get_dte("2024-01-10") == 9
    
    # Dates in the past should return 0 (max(0, ...))
    assert get_dte("2023-12-31") == 0
    
    # Invalid date returns 30
    assert get_dte("invalid") == 30

def test_main_no_args():
    # Should run with default args without crashing
    pass

def test_main_with_mocked_yfinance(monkeypatch):
    class MockTicker:
        def __init__(self, ticker):
            self.ticker = ticker
            
        @property
        def options(self):
            if self.ticker == "INVALID":
                return ()
            # Return one valid short-term, one valid long-term
            return ("2024-01-03", "2024-02-05")

    class MockYF:
        Ticker = MockTicker
        
    # We also need to mock yfinance import inside main since it's inline
    import sys
    sys.modules["yfinance"] = MockYF()
    
    # Mock date.today() inside mid_week_planner
    class MockDate(date):
        @classmethod
        def today(cls):
            return cls(2024, 1, 1)
            
        @classmethod
        def fromisoformat(cls, date_string):
            return date.fromisoformat(date_string)
            
    monkeypatch.setattr("analytics.options.mid_week_planner.date", MockDate)

    captured_out = StringIO()
    monkeypatch.setattr(sys, "stdout", captured_out)
    
    exit_code = main(["SPX", "INVALID"])
    assert exit_code == 0
    
    output = captured_out.getvalue()
    
    # Check if SPX output exists
    assert "MID-WEEK PLANNER: SPX" in output
    assert "Wednesday Expiry (2 DTE)" in output
    assert "Cash Settled" in output
    
    # Check if INVALID output exists
    assert "MID-WEEK PLANNER: INVALID" in output
    assert "No options found for INVALID" in output
    
    # Clean up sys.modules
    del sys.modules["yfinance"]
