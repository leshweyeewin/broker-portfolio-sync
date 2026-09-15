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

    # Offline: the API returns nothing, so the pure-cache path is exercised
    # deterministically (without this, the all-past cache triggers a real
    # network fetch and the test depends on yfinance being reachable).
    monkeypatch.setattr(earnings, "_fetch_from_yfinance", lambda t: None)
    monkeypatch.setattr(earnings, "_load_static_cache", lambda: {
        "NVDA": ["2024-02-21", "2024-05-22"]
    })

    dates = earnings.get_earnings_dates("NVDA")
    assert len(dates) == 2
    assert dates[0] == date(2024, 2, 21)
    
    # Check mem cache
    assert "NVDA" in earnings._mem_cache

def test_get_earnings_dates_refetches_when_next_is_imminent(monkeypatch):
    # A cached upcoming date that's soon (<= 10 days) must trigger a re-fetch so a
    # confirmed/moved date is picked up before the event — not kept stale until it
    # lapses. Here the API confirms the date moved by a week.
    from datetime import timedelta
    earnings._mem_cache.clear()
    today = date.today()
    soon = today + timedelta(days=5)
    moved = today + timedelta(days=12)

    prev = today - timedelta(days=80)  # prior quarter, as the real API returns history too
    monkeypatch.setattr(earnings, "_load_static_cache", lambda: {"MU": [soon.isoformat()]})
    monkeypatch.setattr(earnings, "_save_static_cache", lambda data: None)
    monkeypatch.setattr(earnings, "_fetch_from_yfinance", lambda t: [prev, moved])

    dates = earnings.get_earnings_dates("MU")
    assert moved in dates
    assert soon not in dates  # stale estimate replaced by the confirmed date


def test_get_earnings_dates_keeps_distant_future_without_refetch(monkeypatch):
    # A cached upcoming date far out (> 10 days) is authoritative enough — no
    # network call, cache returned as-is.
    from datetime import timedelta
    earnings._mem_cache.clear()
    far = (date.today() + timedelta(days=60)).isoformat()

    def _boom(t):
        raise AssertionError("should not re-fetch a distant future date")
    monkeypatch.setattr(earnings, "_fetch_from_yfinance", _boom)
    monkeypatch.setattr(earnings, "_load_static_cache", lambda: {"AAPL": [far]})

    dates = earnings.get_earnings_dates("AAPL")
    assert [d.isoformat() for d in dates] == [far]


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
