import pytest
from datetime import date
import analytics.earnings.earnings as earnings

def test_is_near_earnings(monkeypatch):
    monkeypatch.setattr(earnings, "get_earnings_dates", lambda t: [
        date(2024, 1, 10),
        date(2024, 4, 10)
    ])
    
    assert earnings.is_near_earnings("AAPL", date(2024, 1, 10))
    assert earnings.is_near_earnings("AAPL", date(2024, 1, 9))
    assert earnings.is_near_earnings("AAPL", date(2024, 1, 11))
    
    # 2 days away, window default is 1
    assert not earnings.is_near_earnings("AAPL", date(2024, 1, 8))
    
    # Custom window
    assert earnings.is_near_earnings("AAPL", date(2024, 1, 8), window_days=2)

def test_get_earnings_dates_from_cache(monkeypatch):
    # clear memory cache
    earnings._mem_cache.clear()
    
    monkeypatch.setattr(earnings, "_load_static_cache", lambda: {
        "NVDA": ["2024-02-21", "2024-05-22"]
    })
    
    dates = earnings.get_earnings_dates("NVDA")
    assert len(dates) == 2
    assert dates[0] == date(2024, 2, 21)
    
    # Check mem cache
    assert "NVDA" in earnings._mem_cache

def test_refresh_earnings_cache(monkeypatch):
    earnings._mem_cache.clear()
    
    disk_data = {"NVDA": ["2024-02-21"]}
    monkeypatch.setattr(earnings, "_load_static_cache", lambda: disk_data)
    
    def mock_save(data):
        disk_data.update(data)
    monkeypatch.setattr(earnings, "_save_static_cache", mock_save)
    
    monkeypatch.setattr(earnings, "_fetch_from_yfinance", lambda t: [
        date(2024, 2, 21),
        date(2024, 5, 22)
    ])
    
    counts = earnings.refresh_earnings_cache(["NVDA"])
    
    assert counts["NVDA"] == 2
    assert "2024-05-22" in disk_data["NVDA"]
    # Mem cache should be cleared
    assert "NVDA" not in earnings._mem_cache
