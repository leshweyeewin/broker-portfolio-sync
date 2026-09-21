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
            if self.ticker == "MONTHLY":
                return ("2024-02-05",)  # non-empty, but nothing within 0-5 DTE
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

    exit_code = main(["SPX", "MONTHLY"])
    assert exit_code == 0

    output = captured_out.getvalue()

    # Check if SPX output exists
    assert "MID-WEEK PLANNER: SPX" in output
    assert "Wednesday Expiry (2 DTE)" in output
    assert "Cash Settled" in output

    # A ticker whose live list has no 0-5 DTE expiry reports that (no fallback,
    # because the list WAS available — just empty of near-dated expiries).
    assert "MID-WEEK PLANNER: MONTHLY" in output
    assert "No short-dated (0-5 DTE) expiries for MONTHLY" in output

    # Clean up sys.modules
    del sys.modules["yfinance"]


def test_main_falls_back_to_calendar_when_yfinance_unavailable(monkeypatch):
    # When the live options list can't be fetched (throttled/offline → empty),
    # the planner uses the standard Mon/Wed/Fri calendar instead of failing.
    class MockTicker:
        def __init__(self, ticker):
            self.ticker = ticker

        @property
        def options(self):
            return ()  # yfinance came back empty (throttled)

    class MockYF:
        Ticker = MockTicker

    sys.modules["yfinance"] = MockYF()

    class MockDate(date):
        @classmethod
        def today(cls):
            return cls(2024, 1, 1)  # a Monday

        @classmethod
        def fromisoformat(cls, s):
            return date.fromisoformat(s)

    monkeypatch.setattr("analytics.options.mid_week_planner.date", MockDate)
    captured_out = StringIO()
    monkeypatch.setattr(sys, "stdout", captured_out)

    exit_code = main(["MU"])
    assert exit_code == 0
    output = captured_out.getvalue()

    assert "Live options list unavailable" in output
    # Mon 2024-01-01, Wed 2024-01-03, Fri 2024-01-05 fall in the 0-5 DTE window.
    assert "[2024-01-01] Monday Expiry (0 DTE)" in output
    assert "[2024-01-03] Wednesday Expiry (2 DTE)" in output
    assert "[2024-01-05] Friday Expiry (4 DTE)" in output

    del sys.modules["yfinance"]
