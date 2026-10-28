"""Daily price bars and a tiny CSV loader."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from adapters.base import dec


@dataclass(frozen=True)
class Bar:
    day: date
    open: Decimal
    close: Decimal


def load_csv(path: str | Path) -> list[Bar]:
    """Read ``date,open,close`` (extra columns ignored; header names case-insensitive)."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = [{k.strip().lower(): v for k, v in r.items()} for r in csv.DictReader(fh)]
    bars = [Bar(date.fromisoformat(r["date"][:10]), dec(r["open"]), dec(r["close"])) for r in rows]
    bars.sort(key=lambda b: b.day)
    if len({b.day for b in bars}) != len(bars):
        raise ValueError("duplicate dates in price file")
    return bars
