"""Pre-trade check of a candidate against the written trading rules
(``docs/trading-rules.md``): pure arithmetic, advice-free, never touches a broker.

    python -m analytics.risk.rules_gate AMZN --max-loss 67 --width 1 --equity 1330

Account size comes from the environment so Telegram/digest output can show the
check with no extra input:  ACCOUNT_EQUITY, ACCOUNT_CASH, RULE_MAX_WIDTH (default 1),
BANNED_UNDERLYINGS (comma list, added to the built-in leveraged/inverse set).
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

# Rule 2: leveraged / inverse single-stock & index ETFs are banned underlyings.
BANNED_UNDERLYINGS = frozenset({
    "SNDQ", "MULL", "TSLL", "TSLQ", "TSLS", "TSDD", "NVDL", "NVDD", "NVDX", "NVDQ", "CONL", "CONI",
    "MSTU", "MSTZ", "AMZU", "AMZD", "AAPU", "AAPD", "GGLL", "GOOX", "FBL", "MSFU", "MSFD", "BABX",
    "SOXL", "SOXS", "TQQQ", "SQQQ", "UPRO", "SPXU", "SPXL", "SPXS", "UVXY", "SVXY", "LABU", "LABD",
    "FNGU", "FNGD", "TECL", "TECS", "BULZ", "BERZ", "YINN", "YANG",
})
HARD_RISK_PCT = Decimal("0.05")    # never exceed
TARGET_RISK_PCT = Decimal("0.02")  # target, enforced by a stop


@dataclass(frozen=True)
class Check:
    status: str   # pass | warn | fail
    text: str


@dataclass(frozen=True)
class GateResult:
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    def lines(self) -> list[str]:
        icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
        return [f"{icon[c.status]} {c.text}" for c in self.checks]

    def summary(self) -> str:
        """One compact tag for a list line, e.g. ``✅ rules`` or ``❌ rules: width 5 > 1; max loss 30% of equity``."""
        bad = [c.text for c in self.checks if c.status != "pass"]
        if not bad:
            return "✅ rules"
        return ("❌" if not self.ok else "⚠️") + " rules: " + "; ".join(bad)


@dataclass(frozen=True)
class Limits:
    equity: Optional[Decimal] = None
    cash: Optional[Decimal] = None
    max_width: Decimal = Decimal("1")
    banned: frozenset = BANNED_UNDERLYINGS


def limits_from_env(env=None) -> Limits:
    env = os.environ if env is None else env

    def num(name):
        v = (env.get(name) or "").strip()
        try:
            return Decimal(v) if v else None
        except Exception:  # noqa: BLE001 - a bad env value must not break an alert
            return None

    extra = {t.strip().upper() for t in (env.get("BANNED_UNDERLYINGS") or "").split(",") if t.strip()}
    return Limits(num("ACCOUNT_EQUITY"), num("ACCOUNT_CASH"), num("RULE_MAX_WIDTH") or Decimal("1"),
                  BANNED_UNDERLYINGS | extra)


def check_trade(
    ticker: str,
    *,
    max_loss: Optional[Decimal | float | int],   # total dollars, all contracts; None = unbounded
    width: Optional[Decimal | float | int] = None,
    contracts: int = 1,
    cash_needed: Optional[Decimal | float | int] = None,
    limits: Optional[Limits] = None,
) -> GateResult:
    lim = limits or limits_from_env()
    d = lambda v: None if v is None else Decimal(str(v))
    max_loss, width, cash_needed = d(max_loss), d(width), d(cash_needed)
    out: list[Check] = []

    if max_loss is None:
        out.append(Check("fail", "undefined risk: naked short option (rule 1: defined-risk only)"))
    if ticker.upper() in lim.banned:
        out.append(Check("fail", f"{ticker.upper()} is a banned leveraged/inverse underlying (rule 2)"))
    if width is not None and width > lim.max_width:
        out.append(Check("fail", f"width {width:g} > {lim.max_width:g} (rule 4)"))
    if contracts > 1:
        out.append(Check("fail", f"{contracts} contracts; rule 4 is 1 contract"))
    if max_loss is not None:
        if lim.equity:
            pct = max_loss / lim.equity
            if pct > HARD_RISK_PCT:
                out.append(Check("fail", f"max loss ${max_loss:,.0f} = {pct:.1%} of equity (never exceed 5%)"))
            elif pct > TARGET_RISK_PCT:
                out.append(Check("warn", f"max loss ${max_loss:,.0f} = {pct:.1%} of equity > 2%: needs a hard stop"))
        else:
            out.append(Check("warn", f"max loss ${max_loss:,.0f}: set ACCOUNT_EQUITY to check the 2%/5% rule"))
    if cash_needed is not None and lim.cash is not None and cash_needed > lim.cash:
        out.append(Check("fail", f"needs ${cash_needed:,.0f} cash > ${lim.cash:,.0f} available (rule 6)"))
    if not out:
        out.append(Check("pass", "passes the written rules"))
    return GateResult(tuple(out))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check a trade against docs/trading-rules.md.")
    ap.add_argument("ticker")
    ap.add_argument("--max-loss", type=float, help="total $ max loss (omit for naked/undefined)")
    ap.add_argument("--width", type=float)
    ap.add_argument("--contracts", type=int, default=1)
    ap.add_argument("--cash-needed", type=float)
    ap.add_argument("--equity", type=float)
    ap.add_argument("--cash", type=float)
    a = ap.parse_args(argv)
    lim = limits_from_env()
    lim = Limits(Decimal(str(a.equity)) if a.equity else lim.equity, Decimal(str(a.cash)) if a.cash else lim.cash,
                 lim.max_width, lim.banned)
    print("\n".join(check_trade(a.ticker, max_loss=a.max_loss, width=a.width, contracts=a.contracts,
                                cash_needed=a.cash_needed, limits=lim).lines()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
