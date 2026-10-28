from datetime import date, timedelta
from decimal import Decimal

from analytics.backtest.bars import Bar
from analytics.backtest.evidence import evidence_line, format_evidence, load_cache, refresh


def _ev(asof):
    return {"asof": asof, "bars": 700, "first": "2023-10-01", "last": "2026-10-01", "provider": "x",
            "rules": {"mean-reversion": {"trades": 9, "win_rate": 0.56, "erases": 1.4, "return_pct": 6.0, "mdd_pct": 4.0, "avg_bars": 8},
                      "trend": {"trades": 0, "win_rate": None, "erases": None, "return_pct": 0.0, "mdd_pct": 0.0, "avg_bars": None}}}


def test_refresh_caches_skips_fresh_and_survives_failures(tmp_path):
    path = tmp_path / "ev.json"
    calls = []

    def fetch(t, today=None):
        calls.append(t)
        if t == "BAD":
            raise RuntimeError("no data")
        return _ev(today.isoformat())

    today = date(2026, 10, 28)
    assert refresh(["NVDA", "BAD"], path=path, today=today, fetch=fetch) == 1
    assert refresh(["NVDA"], path=path, today=today + timedelta(days=2), fetch=fetch) == 0   # fresh
    assert refresh(["NVDA"], path=path, today=today + timedelta(days=9), fetch=fetch) == 1   # stale
    assert list(load_cache(path)) == ["NVDA"] and calls.count("NVDA") == 2


def test_evidence_line_labels_small_samples(tmp_path):
    path = tmp_path / "ev.json"
    refresh(["NVDA"], path=path, today=date(2026, 10, 28), fetch=lambda t, today=None: _ev("2026-10-28"))
    line = evidence_line("nvda", path=path)
    assert "mean-reversion: 9 trades, 56% win, loss erases 1.4 wins" in line
    assert "trend: no trades" in line and "context only" in line
    assert evidence_line("MISSING", path=path) == ""


def test_evidence_for_runs_both_rules_on_supplied_bars(monkeypatch):
    from analytics.backtest import evidence
    d0 = date(2024, 1, 1)
    bars = [Bar(d0 + timedelta(days=i), Decimal(100 + (i % 7)), Decimal(100 + (i % 5))) for i in range(120)]
    monkeypatch.setattr(evidence, "fetch_bars", lambda *a, **k: (bars, "stub"))
    ev = evidence.evidence_for("ZZZ", today=date(2024, 6, 1))
    assert set(ev["rules"]) == {"mean-reversion", "trend"} and ev["provider"] == "stub"
    assert "Rule evidence" in format_evidence("ZZZ", ev)
