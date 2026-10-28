"""Defined-risk spread backtests: modelled (Black-Scholes) or snapshot-priced.

Same signal timing as the stock engine: signal at the close of bar ``i``, fill
at the open of ``i+1``.  Stop/target are evaluated on closes and executed at
the next open.  Spread value ``V`` is what you would receive selling the
position (credit spreads are negative); P/L per share = ``V_close - V_open``.

* ``pricing="modelled"``: strikes from target delta, premiums from Black-Scholes
  with trailing realised vol x ``iv_mult``.  No skew, no real bid/ask.
* ``pricing="snapshot"``: real expiry/strikes/quotes from stored chain
  snapshots (see ``snapshots.py``); signals without a usable snapshot are
  skipped and counted, never invented.  Entries fill at the natural side
  (sell at bid, buy at ask) unless ``fill_mode="mid"``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from math import floor
from typing import Callable, Literal, Sequence

from analytics.backtest.bars import Bar
from analytics.backtest.engine import BacktestResult, Trade
from analytics.backtest.pricing import bs_price, realized_vol, strike_for_delta, VOL_FLOOR
from analytics.backtest.rules import RuleSpec
from analytics.backtest.snapshots import SnapshotLibrary
from analytics.options.option_chain import OptionChainSnapshot, evaluate_quote_quality

Kind = Literal["put_credit", "call_credit", "bull_call_debit", "bear_put_debit"]
_BULL_PUT, _CALL = "put", "call"


@dataclass(frozen=True)
class SpreadStructure:
    kind: Kind
    width: float = 1.0           # strike distance in dollars
    target_delta: float | None = None   # |delta| of the delta-picked leg
    dte: int = 35                # modelled calendar days to expiry
    dte_min: int = 30            # snapshot mode expiry window
    dte_max: int = 45
    strike_step: float = 1.0

    @property
    def is_credit(self) -> bool:
        return self.kind in ("put_credit", "call_credit")

    @property
    def delta(self) -> float:
        return self.target_delta if self.target_delta is not None else (0.20 if self.is_credit else 0.50)

    @property
    def right(self) -> str:
        return _BULL_PUT if self.kind in ("put_credit", "bear_put_debit") else _CALL

    def label(self) -> str:
        return f"{self.kind} ${self.width:g}-wide, delta {self.delta:.2f}, ~{self.dte}d"

    def legs(self, anchor: float) -> list[tuple[str, float, int]]:
        """(right, strike, +1 long / -1 short) given the delta-picked strike."""
        w, r = self.width, self.right
        return {
            "put_credit": [(r, anchor, -1), (r, anchor - w, 1)],
            "call_credit": [(r, anchor, -1), (r, anchor + w, 1)],
            "bull_call_debit": [(r, anchor, 1), (r, anchor + w, -1)],
            "bear_put_debit": [(r, anchor, 1), (r, anchor - w, -1)],
        }[self.kind]


@dataclass
class _Opened:
    legs: list[tuple[str, float, int]]
    expiry: date
    v_open: float                      # per share, after entry costs
    max_loss: float                    # per share
    value: Callable[[int, float, bool], tuple[float, bool]]  # (idx, spot, at_open) -> (V, modelled?)


def _round_step(x: float, step: float, down: bool) -> float:
    n = x / step
    return (floor(n) if down else -floor(-n)) * step


def _intrinsic(legs, spot: float) -> float:
    return sum(sgn * (max(spot - k, 0.0) if r == "call" else max(k - spot, 0.0)) for r, k, sgn in legs)


def _max_loss(st: SpreadStructure, v_open: float) -> float:
    return max(st.width + v_open, 0.0) if st.is_credit else v_open  # credit: width - credit; debit: debit


# ----------------------------- modelled opener ------------------------------ #
def _open_modelled(bars, i, st: SpreadStructure, iv_mult: float, slip: float):
    vol = realized_vol(bars, i - 1)
    if vol is None:
        return "insufficient history"
    vol = max(vol * iv_mult, VOL_FLOOR)
    spot = float(bars[i].open)
    expiry = bars[i].day + timedelta(days=st.dte)
    t = st.dte / 365.0
    is_call = st.right == "call"
    raw = strike_for_delta(spot, t, vol, st.delta, is_call)
    anchor = _round_step(raw, st.strike_step, down=st.kind in ("put_credit", "bull_call_debit"))
    legs = st.legs(anchor)
    if any(k <= 0 for _, k, _ in legs):
        return "strike out of range"

    def value(idx, s, at_open):
        tt = (expiry - bars[idx].day).days / 365.0
        return sum(sgn * bs_price(s, k, tt, vol, r == "call") for r, k, sgn in legs), True

    v0 = value(i, spot, True)[0] - slip      # pay slippage on entry
    if st.is_credit and v0 >= 0:
        return "no credit"
    return _Opened(legs, expiry, v0, _max_loss(st, v0), value)


# ----------------------------- snapshot opener ------------------------------ #
def _find_snapshot(lib: SnapshotLibrary, ticker: str, bars, i) -> OptionChainSnapshot | None:
    for day in (bars[i].day, bars[i - 1].day):
        snap = lib.get(ticker, day)
        if snap and snap.underlying_price is not None:
            if abs(float(snap.underlying_price) / float(bars[i].open) - 1) <= 0.03:
                return snap
    return None


def _open_snapshot(bars, i, st: SpreadStructure, lib, ticker, fill_mode, slip, min_oi, max_spread_pct):
    snap = _find_snapshot(lib, ticker, bars, i)
    if snap is None:
        return "no snapshot"
    today = bars[i].day
    expiries = sorted({q.contract.expiry for q in snap.quotes
                       if st.dte_min <= (q.contract.expiry - today).days <= st.dte_max},
                      key=lambda e: abs((e - today).days - st.dte))
    if not expiries:
        return "no expiry in window"
    expiry = expiries[0]
    by_key = {(q.contract.right, float(q.contract.strike)): q
              for q in snap.quotes if q.contract.expiry == expiry}
    spot = float(snap.underlying_price)
    t = max((expiry - snap.as_of.date()).days, 1) / 365.0
    cands = [q for (r, _), q in by_key.items() if r == st.right]
    if not cands:
        return "no quotes for right"

    def delta_of(q):
        if q.delta is not None:
            return abs(float(q.delta))
        if q.implied_volatility:
            from analytics.screening.screener import _bs_delta
            return abs(_bs_delta(spot, float(q.contract.strike), (expiry - snap.as_of.date()).days,
                                 float(q.implied_volatility), st.right == "call"))
        return None

    scored = [(abs(d - st.delta), q) for q in cands if (d := delta_of(q)) is not None]
    if not scored:
        return "no delta/iv in snapshot"
    anchor = float(min(scored, key=lambda x: x[0])[1].contract.strike)
    legs = st.legs(anchor)
    quotes = []
    for r, k, sgn in legs:
        q = by_key.get((r, k))
        if q is None:
            return "leg strike not listed"
        ql = evaluate_quote_quality(q, min_open_interest=min_oi, max_spread_pct=Decimal(str(max_spread_pct)))
        if not ql.tradable:
            return "illiquid leg"
        quotes.append((q, sgn))

    def entry_v():
        total = 0.0
        for q, sgn in quotes:
            mid = float(q.midpoint)
            px = mid if fill_mode == "mid" else (float(q.ask) if sgn > 0 else float(q.bid))
            total += sgn * px
        return total

    v0 = entry_v() - (0.0 if fill_mode == "natural" else slip)
    if st.is_credit and v0 >= 0:
        return "no credit"
    ivs = [float(q.implied_volatility) for q, _ in quotes if q.implied_volatility]
    vol = max(sum(ivs) / len(ivs), VOL_FLOOR) if ivs else max(realized_vol(bars, i - 1) or VOL_FLOOR, VOL_FLOOR)

    def value(idx, s, at_open):
        later = lib.get(ticker, bars[idx].day)
        if later is not None:
            index = {(x.contract.expiry, x.contract.right, float(x.contract.strike)): x for x in later.quotes}
            mids = [(index.get((expiry, r, k)), sgn) for r, k, sgn in legs]
            if all(x is not None and x.midpoint is not None for x, _ in mids):
                return sum(sgn * float(x.midpoint) for x, sgn in mids), False
        tt = (expiry - bars[idx].day).days / 365.0
        return sum(sgn * bs_price(s, k, tt, vol, r == "call") for r, k, sgn in legs), True

    return _Opened(legs, expiry, v0, _max_loss(st, v0), value)


# --------------------------------- engine ----------------------------------- #
def run_option_backtest(
    bars: Sequence[Bar],
    rule: RuleSpec,
    structure: SpreadStructure,
    *,
    ticker: str = "",
    pricing: Literal["modelled", "snapshot"] = "modelled",
    capital: Decimal | str | int = 10000,
    risk_pct: float = 0.05,
    fee_per_contract: float = 1.0,
    slippage_per_share: float = 0.03,
    iv_mult: float = 1.15,
    stop_multiple: float | None = 1.0,     # credit spreads: close when loss >= x * credit
    profit_target: float | None = 0.5,     # credit spreads: close at x of the credit captured
    fill_mode: Literal["natural", "mid"] = "natural",
    snapshots: SnapshotLibrary | None = None,
    min_open_interest: int = 100,
    max_spread_pct: float = 0.30,
) -> BacktestResult:
    if len(bars) < 3:
        raise ValueError("need at least three bars")
    if pricing == "snapshot" and not (snapshots and ticker):
        raise ValueError("snapshot pricing needs a SnapshotLibrary and ticker")
    realised = float(capital)          # capital + closed P/L net of fees
    pos: _Opened | None = None
    contracts = 0
    entry_idx = 0
    pend_entry = pend_exit = None  # type: ignore[assignment]
    pend_exit_note = ""
    fees_open = 0.0
    trades: list[Trade] = []
    equity: list[tuple[date, Decimal]] = []
    skipped: Counter[str] = Counter()
    signals = in_market = 0

    def close(idx: int, v_close: float, note: str, modelled: bool, pay_slip: bool) -> None:
        nonlocal pos, contracts, realised, fees_open
        v = v_close - (slippage_per_share if pay_slip else 0.0)
        fees = fees_open + (fee_per_contract * 2 * contracts if pay_slip else 0.0)
        pnl = (v - pos.v_open) * 100 * contracts - fees
        risk = pos.max_loss * 100 * contracts
        realised += pnl
        trades.append(Trade(bars[entry_idx].day, bars[idx].day, contracts, Decimal(str(round(-pos.v_open, 4))),
                            Decimal(str(round(-v, 4))), Decimal(str(round(pnl, 2))),
                            Decimal(str(round(pnl / risk, 4))) if risk else Decimal(0), idx - entry_idx,
                            note == "forced", note + (" modelled_exit" if modelled and pricing == "snapshot" else "")))
        pos, contracts, fees_open = None, 0, 0.0

    for i, bar in enumerate(bars):
        open_px = float(bar.open)
        if pos is not None and pend_exit:
            v, modelled = pos.value(i, open_px, True)
            close(i, v, pend_exit_note, modelled, True)
            pend_exit = None
        elif pos is None and pend_entry:
            pend_entry = False
            if pricing == "modelled":
                res = _open_modelled(bars, i, structure, iv_mult, slippage_per_share)
            else:
                res = _open_snapshot(bars, i, structure, snapshots, ticker, fill_mode,
                                     slippage_per_share, min_open_interest, max_spread_pct)
            if isinstance(res, str):
                skipped[res] += 1
            else:
                n = int(floor(realised * risk_pct / (res.max_loss * 100))) if res.max_loss > 0 else 0
                if n < 1:
                    skipped[f"max loss ${res.max_loss * 100:,.0f} > {risk_pct:.0%} risk budget"] += 1
                else:
                    pos, contracts, entry_idx = res, n, i
                    fees_open = fee_per_contract * 2 * n

        mark = None
        if pos is not None:
            in_market += 1
            last_of_data = i == len(bars) - 1
            expired = bar.day == pos.expiry or (i + 1 < len(bars) and bars[i + 1].day > pos.expiry)
            if expired:
                close(i, _intrinsic(pos.legs, float(bar.close)), "expiry", False, False)
            elif last_of_data:
                v, modelled = pos.value(i, float(bar.close), False)
                close(i, v, "forced", modelled, False)
            else:
                v, _ = pos.value(i, float(bar.close), False)
                mark = v
                credit = -pos.v_open
                if pos.v_open < 0:
                    pnl_ps = v - pos.v_open
                    if stop_multiple is not None and pnl_ps <= -stop_multiple * credit:
                        pend_exit, pend_exit_note = True, "stop"
                    elif profit_target is not None and pnl_ps >= profit_target * credit:
                        pend_exit, pend_exit_note = True, "target"
                if not pend_exit and rule.exit(bars, i):
                    pend_exit, pend_exit_note = True, "rule"
        if pos is None and not pend_entry and i + 1 < len(bars) and rule.entry(bars, i):
            signals += 1
            pend_entry = True
        eq = realised + (0.0 if pos is None else (mark - pos.v_open) * 100 * contracts - fees_open
                         if mark is not None else -fees_open)
        equity.append((bar.day, Decimal(str(round(eq, 2)))))

    return BacktestResult(rule, Decimal(str(capital)), tuple(trades), tuple(equity), in_market,
                          bars[0].day, bars[-1].day, pricing, structure.label(), f"{risk_pct:.0%} of equity as max loss", signals,
                          tuple(sorted(skipped.items(), key=lambda kv: -kv[1])))
