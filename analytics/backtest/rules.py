"""Rule specs and signal functions.

A ``RuleSpec`` is the "system sentence" as data: when [signal] occurs, trade
[structure], risk [size], exit on [rule], review [metrics].  Signals are
evaluated on the close of bar ``i``; the engine fills at the open of ``i+1`` so
a trade can never fill before its signal exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from statistics import fmean, pstdev
from typing import Callable, Sequence

from analytics.backtest.bars import Bar

# (bars up to and including i, i) -> bool
Signal = Callable[[Sequence[Bar], int], bool]


@dataclass(frozen=True)
class RuleSpec:
    name: str
    idea: str
    entry_text: str
    exit_text: str
    entry: Signal = field(repr=False)
    exit: Signal = field(repr=False)
    structure: str = "long stock"
    size_fraction: Decimal = Decimal("0.25")  # share of equity per entry
    version: str = "1"

    def system_sentence(self) -> str:
        return (f"When {self.entry_text}, trade {self.structure}, risk "
                f"{self.size_fraction:.0%} of equity, exit when {self.exit_text}.")


def bollinger(bars: Sequence[Bar], i: int, period: int, k: float) -> tuple[float, float, float] | None:
    """(lower, mean, upper) at bar ``i``; ``None`` until ``period`` closes exist."""
    if i + 1 < period:
        return None
    closes = [float(b.close) for b in bars[i + 1 - period:i + 1]]
    mean = fmean(closes)
    sd = pstdev(closes)
    return mean - k * sd, mean, mean + k * sd


def mean_reversion(period: int = 20, k: float = 2.0, size_fraction: str = "0.25") -> RuleSpec:
    """Close below lower band -> enter; close above the mean -> exit."""
    def entry(bars, i):
        band = bollinger(bars, i, period, k)
        return band is not None and float(bars[i].close) < band[0]

    def exit_(bars, i):
        band = bollinger(bars, i, period, k)
        return band is not None and float(bars[i].close) > band[1]

    return RuleSpec(
        name=f"mean_reversion_bb{period}_{k:g}",
        idea="stretched price may return to its average",
        entry_text=f"close is below the lower Bollinger Band ({period}, {k:g})",
        exit_text="close is above the mean",
        entry=entry, exit=exit_, size_fraction=Decimal(size_fraction),
    )


def trend_following(period: int = 20, k: float = 2.0, size_fraction: str = "0.25") -> RuleSpec:
    """Close above upper band -> enter; close below the mean -> exit."""
    def entry(bars, i):
        band = bollinger(bars, i, period, k)
        return band is not None and float(bars[i].close) > band[2]

    def exit_(bars, i):
        band = bollinger(bars, i, period, k)
        return band is not None and float(bars[i].close) < band[1]

    return RuleSpec(
        name=f"trend_following_bb{period}_{k:g}",
        idea="strength may continue",
        entry_text=f"close is above the upper Bollinger Band ({period}, {k:g})",
        exit_text="close is below the mean",
        entry=entry, exit=exit_, size_fraction=Decimal(size_fraction),
    )


DEFAULT_RULES = {"mean_reversion": mean_reversion, "trend": trend_following}
