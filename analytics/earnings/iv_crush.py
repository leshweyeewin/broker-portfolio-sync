"""IV-Crush earnings screener — the codified moomoo Options-Playbook framework.

Automates the feasible half of the playbook's 4-step earnings filter across a
watchlist. For each name with earnings inside the horizon it computes:

  * **Step 4 — Implied vs Historical move** (the headline edge): compares the
    option market's implied Expected Move to how far the stock *actually* moves
    on earnings (``analytics.earnings.earnings_move``). ``RICH`` = market pricing more
    than history → premium worth selling; ``CHEAP`` = selling cheap → skip.
  * **Directional bias** from the trend (SMA-20 vs SMA-50 stack) → the credit
    strategy the playbook pairs with that bias.
  * **Expected-Move bounds** (upper/lower price) so the short leg can be placed
    *outside* the 1-SD move, per the deck's strike rule.

What it deliberately does NOT fake: Steps 1–3 (IV Percentile, crush consistency,
crush magnitude) need an IV history that free data doesn't retain — those light
up once the IV logger (tool #3) has accumulated snapshots. Each candidate says
so rather than inventing a letter grade from one step.

Read-only. Run:  python -m analytics.earnings.iv_crush NVDA CRM CRWD COST
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

from analytics.earnings.earnings_move import historical_earnings_move, implied_vs_historical
from analytics.earnings.iv_crush_history import historical_iv_crush

from analytics.options.strategies import build_put_credit_spreads, build_call_credit_spreads, build_iron_condors, SpreadFilters
from analytics.screening.screener import _build_quote_client, fetch_option_chain
from analytics.options.option_chain import OptionChainSnapshot, OptionQuote, OptionContract
from datetime import timezone, datetime
from analytics.screening.market_scan import (
    _annualized_realized_vol,
    _estimate_expected_move,
    get_upcoming_earnings,
)

log = logging.getLogger(__name__)

DEFAULT_DAYS_AHEAD = 14
DEFAULT_LOOKBACK = 8

# Strategy the playbook pairs with each directional bias (Step-3 "The How").
_STRATEGY_BY_BIAS = {
    "Bullish": "Put Credit Spread (sell put below lower EM)",
    "Bearish": "Call Credit Spread (sell call above upper EM)",
    "Neutral": "Iron Condor (sell both sides outside EM)",
}

# Step-4 edge → screener signal.
_SIGNAL_BY_EDGE = {
    "RICH": "SELL — rich premium",
    "FAIR": "FAIR — fair value",
    "CHEAP": "SKIP — selling cheap",
}


@dataclass
class IVCrushCandidate:
    ticker: str
    earnings_date: date
    days_left: int
    price: Optional[float] = None
    implied_move_pct: Optional[float] = None
    em_lower: Optional[float] = None
    em_upper: Optional[float] = None
    hist_move_pct: Optional[float] = None      # avg abs earnings gap (Step-4 history)
    hist_bias: str = ""                        # e.g. "6↓/2↑"
    edge: Optional[str] = None                 # RICH / FAIR / CHEAP
    bias: str = "Neutral"                      # trend bias
    strategy: str = ""
    current_iv: Optional[float] = None
    iv_percentile: Optional[float] = None       # Step 1
    crush_consistency: Optional[float] = None   # Step 2 — fraction 0..1 IV fell post-earnings
    crush_magnitude_pct: Optional[float] = None # Step 3 — avg post-earnings IV drop %

    @property
    def signal(self) -> str:
        return _SIGNAL_BY_EDGE.get(self.edge or "", "NO DATA — need history")

    @property
    def grade(self) -> int:
        """Count of GREEN playbook steps (0–4)."""
        return playbook_grade(self)[0]

    @property
    def verdict(self) -> str:
        """FOCUS / WATCH / SKIP from the green count."""
        return playbook_grade(self)[1]

    def line(self) -> str:
        when = "Today!" if self.days_left == 0 else (
            "Tomorrow!" if self.days_left == 1 else f"in {self.days_left}d")
        bounds = (f" · EM ${self.em_lower:.2f}–${self.em_upper:.2f}"
                  if self.em_lower is not None else "")
        imp = f"±{self.implied_move_pct:.1f}%" if self.implied_move_pct is not None else "n/a"
        hist = f"±{self.hist_move_pct:.1f}%" if self.hist_move_pct is not None else "n/a"
        ivp = f"IVP {self.iv_percentile:.0f}%" if self.iv_percentile is not None else "IVP n/a"
        crush = (
            f" · crush {self.crush_magnitude_pct:.1f}%×{self.crush_consistency * 100:.0f}%"
            if self.crush_magnitude_pct is not None and self.crush_consistency is not None
            else "")
        return (f"[{self.grade}/4 {self.verdict}] {self.ticker} — "
                f"{self.earnings_date} ({when}) · {self.signal}\n"
                f"   implied {imp} vs hist {hist} {self.hist_bias} · {ivp}{crush} · "
                f"{self.bias} → {self.strategy}{bounds}")


def _sma(vals: list[float], window: int) -> Optional[float]:
    return statistics.fmean(vals[-window:]) if len(vals) >= window else None


def trend_bias(closes: list[float]) -> str:
    """Directional bias from the SMA-20 / SMA-50 stack (needs >=50 closes).

    ``Bullish`` when price > SMA20 > SMA50, ``Bearish`` when price < SMA20 <
    SMA50, else ``Neutral``. Neutral is also the safe default without enough data.
    """
    price = closes[-1] if closes else None
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    if price is None or sma20 is None or sma50 is None:
        return "Neutral"
    if price > sma20 > sma50:
        return "Bullish"
    if price < sma20 < sma50:
        return "Bearish"
    return "Neutral"


def em_bounds(price: float, implied_move_pct: float) -> tuple[float, float]:
    """Lower/upper 1-SD Expected-Move price bounds for strike placement."""
    delta = price * implied_move_pct / 100.0
    return round(price - delta, 2), round(price + delta, 2)


def _load_iv_history(ticker: str) -> dict[str, float]:
    history_file = Path(__file__).resolve().parent.parent / "data" / "iv_history.json"
    if not history_file.exists():
        return {}
    try:
        with open(history_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get(ticker.upper(), {})
    except Exception:
        return {}


def compute_iv_percentile(
    current_iv: float,
    history: dict[str, float],
    *,
    window_days: int = 365,
    today: Optional[date] = None,
) -> Optional[float]:
    """% of the last ``window_days`` of IV snapshots that sat below ``current_iv``.

    ``history`` maps ISO-date → IV fraction (as written by ``analytics.earnings.iv_logger``).
    Snapshots older than the trailing window are ignored so a stale year can't skew
    the rank — IV percentile is conventionally a 1-year measure. Returns ``None``
    with fewer than two usable observations in the window.
    """
    if not history:
        return None
    today = today or date.today()
    cutoff = today - timedelta(days=window_days)
    vals: list[float] = []
    for d, iv in history.items():
        try:
            snap = date.fromisoformat(d)
        except (ValueError, TypeError):
            continue
        if snap >= cutoff:
            vals.append(iv)
    if len(vals) < 2:
        return None
    lower = sum(1 for v in vals if v < current_iv)
    return (lower / len(vals)) * 100


def _fetch_snapshot(tk, earnings_date: date) -> tuple[Optional[float], Optional[float], list[float]]:
    """Return (price, implied_move_pct, recent_closes) for one yfinance Ticker."""
    closes: list[float] = []
    price: Optional[float] = None
    try:
        hist = tk.history(period="4mo")
        if hist is not None and not hist.empty:
            closes = [float(c) for c in hist["Close"].dropna().tolist()]
            if closes:
                price = closes[-1]
    except Exception as exc:
        log.debug("History fetch failed: %s", exc)
    fast_info = getattr(tk, "fast_info", None)
    price = getattr(fast_info, "last_price", None) or price
    if not price:
        return None, None, closes
    implied = _estimate_expected_move(tk, earnings_date, float(price))
    return float(price), implied, closes


def earnings_universe(*, include_holdings: bool = True) -> list[str]:
    """Ticker universe for the earnings scan: monitored watchlist + held names.

    Mirrors the daily "Upcoming Earnings" report universe so the on-demand
    ``/spreads`` command and the weekly digest screen the same names you already
    watch. Sheet access is best-effort — on any failure the monitored watchlist
    cache alone is returned, so the scan never depends on the sheet being up.
    """
    names: set[str] = set()
    try:
        from analytics.earnings.earnings import _load_static_cache
        names.update(_load_static_cache().keys())
    except Exception as exc:
        log.debug("earnings watchlist cache unavailable: %s", exc)

    if include_holdings:
        try:
            from sheets.writer import PortfolioWriter, SheetClient
            from config.settings import get_service_account_info, get_spreadsheet_id
            writer = PortfolioWriter(SheetClient(get_service_account_info(), get_spreadsheet_id()))
            for item in writer.read_all_stock_trades():
                names.add(item["trade"].underlying)
            for item in writer.read_all_option_trades():
                names.add(item["trade"].underlying)
        except Exception as exc:
            log.debug("holdings universe unavailable (watchlist-only): %s", exc)

    return sorted(t for t in names if t and t.isalpha() and len(t) <= 6)


def scan_iv_crush(
    tickers: list[str],
    *,
    today: Optional[date] = None,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    lookback: int = DEFAULT_LOOKBACK,
) -> list[IVCrushCandidate]:
    """Screen a watchlist for earnings IV-crush credit-spread setups (best-effort)."""
    today = today or date.today()
    events = get_upcoming_earnings(tickers, today=today, days_ahead=days_ahead)
    if not events:
        return []

    try:
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    except ImportError:
        log.debug("yfinance not installed — cannot screen IV crush")
        return []

    out: list[IVCrushCandidate] = []
    for ev in events:
        cand = IVCrushCandidate(
            ticker=ev.ticker, earnings_date=ev.earnings_date, days_left=ev.days_left)
        try:
            tk = yf.Ticker(ev.ticker)
            price, implied, closes = _fetch_snapshot(tk, ev.earnings_date)
            cand.price = price
            cand.implied_move_pct = implied
            if price and implied:
                cand.em_lower, cand.em_upper = em_bounds(price, implied)
            cand.bias = trend_bias(closes)
            cand.strategy = _STRATEGY_BY_BIAS[cand.bias]

            study = historical_earnings_move(ev.ticker, lookback=lookback, today=today)
            if study:
                cand.hist_move_pct = study.avg_abs_reaction
                cand.hist_bias = f"{study.bear_count}↓/{study.bull_count}↑"
                cand.edge = implied_vs_historical(implied, study.avg_abs_reaction)
                
            iv_history = _load_iv_history(ev.ticker)
            if iv_history:
                from analytics.earnings.iv_logger import fetch_atm_iv
                current_iv = fetch_atm_iv(ev.ticker)
                if current_iv:
                    cand.current_iv = current_iv
                    cand.iv_percentile = compute_iv_percentile(
                        current_iv, iv_history, today=today)


            crush = historical_iv_crush(ev.ticker, lookback=lookback, today=today)
            if crush and crush.n:
                cand.crush_consistency = crush.consistency
                cand.crush_magnitude_pct = crush.avg_crush_pct
                
            # Slice 3: Wire into live provider for Credit Spreads
            if cand.edge in ("RICH", "FAIR") and cand.em_lower and cand.em_upper:
                client = _build_quote_client()
                exps = tk.options
                if exps:
                    # Find first expiry after or on earnings date
                    target_exp = None
                    for exp in exps:
                        exp_dt = date.fromisoformat(exp.replace("/", "-"))
                        if exp_dt >= ev.earnings_date:
                            target_exp = exp
                            break
                            
                    if target_exp:
                        chain = fetch_option_chain(ev.ticker, target_exp, quote_client=client)
                        quotes = []
                        for c in chain:
                            right = str(c.get("right", c.get("option_type", c.get("type", "")))).lower()
                            if right not in ("call", "put"): continue
                            strike = float(c.get("strike", c.get("strike_price", 0)))
                            bid = float(c.get("bid_price", c.get("bid", 0)))
                            ask = float(c.get("ask_price", c.get("ask", 0)))
                            oi = int(c.get("open_interest", 0) or 0)
                            delta = c.get("delta")
                            if delta is not None:
                                delta = float(delta)
                            contract = OptionContract(ev.ticker, ev.ticker, date.fromisoformat(target_exp.replace("/", "-")), strike, right)
                            # delta/OI are required for the credit-spread builder to gate a short
                            # leg; without them every candidate is (correctly) rejected.
                            quotes.append(OptionQuote(contract, datetime.now(timezone.utc), "broker_live" if client else "delayed", bid=bid, ask=ask, open_interest=oi, delta=delta))
                            
                        snap = OptionChainSnapshot(
                            snapshot_id="iv_crush",
                            underlying=ev.ticker,
                            underlying_price=price,
                            quotes=tuple(quotes),
                            as_of=datetime.now(timezone.utc),
                            source="broker_live" if client else "delayed"
                        )
                        
                        # Build strategies
                        if cand.bias == "Bullish":
                            res = build_put_credit_spreads(snap, exp_dt, filters=SpreadFilters(min_credit=Decimal("0.20")))
                            if res.candidates:
                                c = res.candidates[0]
                                cand.strategy += f" [Live: {target_exp} {c.legs[0].strike}/{c.legs[1].strike} | Cr: ${c.net_credit:.2f}]"
                        elif cand.bias == "Bearish":
                            res = build_call_credit_spreads(snap, exp_dt, filters=SpreadFilters(min_credit=Decimal("0.20")))
                            if res.candidates:
                                c = res.candidates[0]
                                cand.strategy += f" [Live: {target_exp} {c.legs[0].strike}/{c.legs[1].strike} | Cr: ${c.net_credit:.2f}]"
                        else:
                            res = build_iron_condors(snap, exp_dt, filters=SpreadFilters(min_credit=Decimal("0.50")))
                            if res.candidates:
                                c = res.candidates[0]
                                cand.strategy += f" [Live: {target_exp} {c.legs[0].strike}/{c.legs[1].strike} & {c.legs[2].strike}/{c.legs[3].strike} | Cr: ${c.net_credit:.2f}]"

        except Exception as exc:
            log.debug("IV-crush scan failed for %s: %s", ev.ticker, exc)
        out.append(cand)

    # Highest playbook grade first, then RICH edge, then soonest earnings.
    edge_rank = {"RICH": 0, "FAIR": 1, "CHEAP": 2, None: 3}
    out.sort(key=lambda c: (-c.grade, edge_rank.get(c.edge, 3), c.days_left, c.ticker))
    return out


def playbook_grade(cand: "IVCrushCandidate") -> tuple[int, str]:
    """Count of GREEN playbook steps → ``(green_count, verdict)``.

    GREEN thresholds (a step whose input is unknown/``None`` counts as NOT green):
      * Step 1 — IV Percentile > 70
      * Step 2 — crush consistency ≥ 0.75
      * Step 3 — average crush magnitude > 10%
      * Step 4 — Step-4 edge is ``RICH``
    Verdict: 4 → ``FOCUS``, 3 → ``WATCH``, ≤2 → ``SKIP``.
    """
    green = 0
    if cand.iv_percentile is not None and cand.iv_percentile > 70:
        green += 1
    if cand.crush_consistency is not None and cand.crush_consistency >= 0.75:
        green += 1
    if cand.crush_magnitude_pct is not None and cand.crush_magnitude_pct > 10:
        green += 1
    if cand.edge == "RICH":
        green += 1
    verdict = "FOCUS" if green == 4 else ("WATCH" if green == 3 else "SKIP")
    return green, verdict


def format_message(candidates: list[IVCrushCandidate]) -> str:
    header = (f"📅 IV-Crush Earnings Screen — {len(candidates)} upcoming "
              f"(Step 4 + bias; IV-rank steps pending logger):")
    return "\n".join([header, ""] + [c.line() for c in candidates])


def main(argv=None) -> int:
    """CLI: ``python -m analytics.earnings.iv_crush NVDA CRM [...] [--days-ahead N]``."""
    import argparse
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="IV-Crush earnings screener.")
    ap.add_argument("tickers", nargs="+", help="Watchlist ticker symbols.")
    ap.add_argument("--days-ahead", type=int, default=DEFAULT_DAYS_AHEAD,
                    help="Earnings horizon in days (default %(default)s).")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK,
                    help="Past earnings events for the history study (default %(default)s).")
    args = ap.parse_args(argv)

    cands = scan_iv_crush(args.tickers, days_ahead=args.days_ahead, lookback=args.lookback)
    if not cands:
        print("No upcoming earnings in horizon for the given tickers.")
        return 0
    print(format_message(cands))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
