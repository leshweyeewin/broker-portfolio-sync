"""Generate the pancherry site's data files from the live portfolio Sheet.

Two deliverables, both written straight into the pancherry repo clone so the
weekly update stops being a hand-edit:

* ``openPositions.ts`` — the current book (net shares + open option legs),
  **fully regenerated** every run. The one thing that survives a regen is a
  ``hidden: true`` flag: curation the user set stays set.
* ``weeklyJournals.ts`` — a new week's entry **appended** as ``published:
  true``, pre-filled with real stats + auto-picked highlights + a factual
  first-draft narrative. Existing (hand-edited) entries are never touched, and a
  re-run for a week already present is a no-op.

Privacy rule (matches lemon8): percentages and share/contract *counts* only —
never dollar amounts or portfolio size. This module reads P/L to rank winners
vs losers and to derive return %, but never emits an absolute figure.

Read-only against the Sheet; the only writes are to the two .ts files. Nothing
here commits or pushes — publishing the public site stays a manual step.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from sheets.writer import (
    STOCKS_HEADERS,
    OPTIONS_HEADERS,
    TAB_STOCKS,
    TAB_OPTIONS,
    DATA_HEADER_ROWS,
    _col_letter,
    sheet_date_to_iso,
)
from lemon8.reader import ClosedPosition, read_closed_positions  # noqa: F401 (re-export convenience)
from lemon8.weekly_job import select_recent_closes

log = logging.getLogger(__name__)

# Buy / Opening Balance add to a lot; everything else (Sell) disposes. Mirrors
# alerting.expiry._ACQUIRE_ACTIONS, read back from the sheet's own strings.
_ACQUIRE_ACTIONS = {"Buy", "Opening Balance"}

DEFAULT_WINDOW_DAYS = 7
MAX_WINNERS = 3
MAX_LOSERS = 2


# --------------------------------------------------------------------------- #
# Open positions — netting
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class OptionLegData:
    type: str          # "Call" | "Put"
    strike: Decimal
    expiry: str        # ISO yyyy-mm-dd
    qty: Decimal       # + long, − short


@dataclass(frozen=True)
class OpenPositionData:
    ticker: str
    shares: Decimal = Decimal(0)   # net equity; − = short
    legs: list[OptionLegData] = field(default_factory=list)


def read_open_positions(client) -> list[OpenPositionData]:
    """Net the Stocks + Options tabs into the current open book.

    Shares net per ticker; option legs net per (ticker, type, strike, expiry).
    A ticker appears if it still holds shares *or* has any open leg. Sorted
    alphabetically for a stable diff.
    """
    shares = _net_shares(client)
    legs = _net_legs(client)

    tickers = {t for t, q in shares.items() if q != 0} | set(legs.keys())
    out: list[OpenPositionData] = []
    for ticker in sorted(tickers):
        out.append(
            OpenPositionData(
                ticker=ticker,
                shares=shares.get(ticker, Decimal(0)),
                legs=sorted(legs.get(ticker, []), key=lambda l: (l.expiry, l.type, l.strike)),
            )
        )
    return out


def _net_shares(client) -> dict[str, Decimal]:
    values, idx = _read_tab(client, TAB_STOCKS, STOCKS_HEADERS)
    net: dict[str, Decimal] = {}
    for row in values:
        ticker = str(_cell(row, idx["Ticker"])).strip()
        qty = _dec(_cell(row, idx["Qty"]))
        if not ticker or qty is None or _is_malformed(ticker):
            continue
        action = str(_cell(row, idx["Action"])).strip()
        signed = qty if action in _ACQUIRE_ACTIONS else -qty
        net[ticker] = net.get(ticker, Decimal(0)) + signed
    return net


def _net_legs(client) -> dict[str, list[OptionLegData]]:
    values, idx = _read_tab(client, TAB_OPTIONS, OPTIONS_HEADERS)
    net: dict[tuple[str, str, Decimal, str], Decimal] = {}
    malformed: set[str] = set()
    for row in values:
        ticker = str(_cell(row, idx["Stock"])).strip()
        strike = _dec(_cell(row, idx["Strike"]))
        qty = _dec(_cell(row, idx["Qty"]))
        expiry = sheet_date_to_iso(_cell(row, idx["Expiry"]))
        if not ticker or strike is None or qty is None or not expiry:
            continue
        if _is_malformed(ticker):
            malformed.add(ticker)
            continue
        otype = _norm_type(str(_cell(row, idx["Type"])))
        action = str(_cell(row, idx["Action"])).strip()
        signed = qty if action in _ACQUIRE_ACTIONS else -qty
        key = (ticker, otype, strike, expiry)
        net[key] = net.get(key, Decimal(0)) + signed

    if malformed:
        log.warning(
            "Skipped %d option leg group(s) with a malformed underlying "
            "(combo symbol in the Stock column): %s",
            len(malformed), ", ".join(sorted(malformed)),
        )

    out: dict[str, list[OptionLegData]] = {}
    for (ticker, otype, strike, expiry), qty in net.items():
        if qty == 0:
            continue
        out.setdefault(ticker, []).append(
            OptionLegData(type=otype, strike=strike, expiry=expiry, qty=qty)
        )
    return out


def _read_tab(client, tab: str, headers: list[str]) -> tuple[list[list[Any]], dict[str, int]]:
    """Return (data rows below the header row, header-name → column index)."""
    col_letter = _col_letter(len(headers))
    values = client.get_values(f"{tab}!A:{col_letter}")
    header_idx = DATA_HEADER_ROWS.get(tab, 1) - 1
    if len(values) <= header_idx:
        return [], {}
    idx = {str(name).strip(): i for i, name in enumerate(values[header_idx])}
    return values[header_idx + 1:], idx


# --------------------------------------------------------------------------- #
# openPositions.ts rendering
# --------------------------------------------------------------------------- #

_OPEN_POSITIONS_HEADER = """\
// Open Positions — current book, grouped by underlying.
//
// AUTO-GENERATED by broker-portfolio-sync (pancherry_export). Every run fully
// regenerates this file from the live sync, so manual edits here are
// overwritten — EXCEPT a `hidden: true` flag on a ticker's first line, which is
// preserved across regens. To hide a position, add `, hidden: true` right after
// `shares:` on its opening line.
//
// Privacy rule: sizes as share/contract counts only — never dollar amounts or
// portfolio weight. `shares` negative = short; leg `qty` negative = short/written.

export interface OptionLeg {
  type: 'Call' | 'Put';
  strike: number;
  expiry: string;   // ISO YYYY-MM-DD
  qty: number;      // negative = short/written
}

export interface OpenPosition {
  ticker: string;
  name?: string;    // broker-reported security name (auto-filled from the sync)
  shares: number;   // net equity; negative = short; 0 = options-only
  legs: OptionLeg[];
  hidden?: boolean; // set true to drop from the grid
}

// Injected by exporter
// export const asOfDate: string = "...";

export const openPositions: OpenPosition[] = [
"""


def render_open_positions_ts(
    positions: list[OpenPositionData],
    *,
    hidden: Optional[set[str]] = None,
    names: Optional[dict[str, str]] = None,
) -> str:
    """Render the whole ``openPositions.ts`` file. ``hidden`` tickers get their
    flag re-applied so user curation survives the regen; ``names`` (ticker →
    security name, from the sync cache) fills the optional ``name`` field."""
    hidden = hidden or set()
    names = names or {}
    
    from datetime import date
    today_str = date.today().isoformat()
    header = _OPEN_POSITIONS_HEADER.replace(
        "// export const asOfDate: string = \"...\";",
        f"export const asOfDate: string = '{today_str}';"
    )
    
    lines = [header]
    for pos in positions:
        head = f"  {{ ticker: '{pos.ticker}'"
        nm = (names.get(pos.ticker) or "").strip()
        if nm:
            head += f", name: {_ts_str(nm)}"
        head += f", shares: {_num(pos.shares)}"
        if pos.ticker in hidden:
            head += ", hidden: true"
        if not pos.legs:
            lines.append(head + ", legs: [] },")
            continue
        lines.append(head + ", legs: [")
        for leg in pos.legs:
            lines.append(
                f"    {{ type: '{leg.type}', strike: {_num(leg.strike)}, "
                f"expiry: '{leg.expiry}', qty: {_num(leg.qty)} }},"
            )
        lines.append("  ] },")
    lines.append("];\n")
    return "\n".join(lines)


def write_open_positions(
    positions: list[OpenPositionData], path: Path, *, names: Optional[dict[str, str]] = None
) -> None:
    """Regenerate ``openPositions.ts`` at ``path``, preserving hidden flags and
    filling security names from the ``names`` map."""
    path = Path(path)
    hidden = _existing_hidden(path)
    path.write_text(render_open_positions_ts(positions, hidden=hidden, names=names), encoding="utf-8")


def _existing_hidden(path: Path) -> set[str]:
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8")
    # Matches a ticker whose opening line also carries `hidden: true`.
    return set(re.findall(r"ticker:\s*'([^']+)'[^\n]*\bhidden:\s*true", text))


# --------------------------------------------------------------------------- #
# Weekly journal — build
# --------------------------------------------------------------------------- #

def build_weekly_journal(
    closed: list[ClosedPosition], *, today: date, window_days: int = DEFAULT_WINDOW_DAYS
) -> dict:
    """Build one ``WeeklyJournal`` entry (as a dict) from the week's closed rows.

    Stats and highlights are real; the narrative is a plain factual first draft
    for the user to rewrite. Emitted with ``published: true``.
    """
    recent = select_recent_closes(closed, today=today, window_days=window_days)

    # "Decided" trades are the ones with a realized P/L we can stand behind. Rows
    # the sync marks Closed but leaves P/L blank aren't counted — so trades always
    # equals wins + losses + break-evens and the win rate is honest.
    decided = [p for p in recent if p.realized_pl is not None]
    winners = sorted([p for p in decided if p.realized_pl > 0], key=lambda p: p.realized_pl, reverse=True)
    losers = sorted([p for p in decided if p.realized_pl < 0], key=lambda p: p.realized_pl)
    win_rate = round(len(winners) / (len(winners) + len(losers)) * 100) if (winners or losers) else 0

    start, end = _week_span(decided or recent, today, window_days)
    iso_year, iso_week, _ = today.isocalendar()
    week_label = _format_span(start, end)

    from collections import defaultdict
    grouped = defaultdict(list)
    for p in decided:
        grouped[p.symbol].append(p)
    
    highlights = []
    # Sort tickers alphabetically to match open positions style
    for ticker in sorted(grouped.keys()):
        trades = grouped[ticker]
        # Sort trades within ticker by P/L descending (winners first)
        trades.sort(key=lambda x: x.realized_pl if x.realized_pl is not None else -999999, reverse=True)
        for p in trades:
            highlights.append(_to_highlight(p))

    return {
        "slug": f"{iso_year}-w{iso_week}",
        "title": f"Weekly Recap · {week_label}",
        "weekOf": week_label,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "summary": _summary(decided, winners, losers, win_rate),
        "body": _body(decided, winners, losers, win_rate),
        "trades": len(decided),
        "wins": len(winners),
        "losses": len(losers),
        "winRatePct": win_rate,
        "highlights": highlights,
        "published": True,
    }


def _dedupe_highlights(highlights: list[dict]) -> list[dict]:
    """Drop repeated (ticker, contract) cards — separate lots of the same contract
    read as duplicates. Keeps the first, which is the best-ranked one."""
    seen: set[tuple] = set()
    out = []
    for h in highlights:
        key = (h["ticker"], h.get("contract", ""), h["direction"])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def _to_highlight(p: ClosedPosition) -> dict:
    win = p.realized_pl is not None and p.realized_pl > 0
    out = {
        "ticker": p.symbol,
        "asset": p.asset,
        "strategy": (p.strategy or ("Option" if p.asset == "option" else "Stock")).strip(),
        "direction": "win" if win else "loss",
        "returnPct": _round_pct(p.return_pct),
        "note": "",
    }
    if p.asset == "option" and p.strike and p.option_type:
        out["contract"] = f"{_strike_money(p.strike)} {p.option_type}"
    return out


def _summary(decided, winners, losers, rate) -> str:
    """One-liner teaser for the journal card (w33 editorial style)."""
    theme = _dominant_theme(decided)
    standout = _standout_phrase(winners)
    
    if theme and standout:
        return f"{theme} {standout} the losers were cut early and cleanly per the risk rules."
    if theme:
        return f"{theme} The losers were cut early and cleanly per the risk rules."
    
    return "A high-activity week. The winners ran on confirmed setups; the losers were cut early and cleanly per the risk rules."


def _body(decided, winners, losers, rate) -> list[str]:
    """Multi-paragraph narrative body."""
    trades = len(decided)
    wins = len(winners)
    losses = len(losers)

    paras: list[str] = []

    # --- Opening: mix breakdown ---
    stocks = [p for p in decided if p.asset == "stock"]
    options = [p for p in decided if p.asset == "option"]
    if stocks and options:
        opener = (f"{trades} positions closed: {len(options)} option trades and "
                  f"{len(stocks)} stock trades — {wins} winners against {losses} losers "
                  f"for a {rate}% hit rate.")
    else:
        asset = "option" if options else "stock"
        opener = (f"{trades} {asset} positions closed: {wins} winners against "
                  f"{losses} losers — a {rate}% hit rate.")
    paras.append(opener)

    # --- Strategy breakdown (what kinds of trades) ---
    strat_line = _strategy_breakdown(decided)
    if strat_line:
        paras.append(strat_line)

    # --- Winners paragraph ---
    if winners:
        top = winners[:MAX_WINNERS]
        labels = ", ".join(f"{_pretty_label(p)} ({_ret_str(p)})" for p in top)
        if len(winners) == 1:
            paras.append(f"The standout winner was {labels}.")
        else:
            verb = "led the winners" if len(winners) > MAX_WINNERS else "were the winners"
            paras.append(f"Top performers: {labels} {verb}.")

    # --- Losers paragraph ---
    if losers:
        top = losers[:MAX_LOSERS]
        labels = ", ".join(f"{_pretty_label(p)} ({_ret_str(p)})" for p in top)
        paras.append(f"Cut for a loss and moved on: {labels}. Discipline over conviction.")

    # --- Ticker concentration ---
    concentration = _ticker_concentration(decided)
    if concentration:
        paras.append(concentration)

    return paras


def _dominant_theme(decided: list) -> str:
    """Identify the dominant strategy and build an editorial theme phrase."""
    from collections import Counter
    strategies = Counter(p.kind for p in decided if p.kind)
    tickers = Counter(p.symbol for p in decided)
    
    if not strategies or not tickers:
        return ""
        
    top_strat, top_count = strategies.most_common(1)[0]
    top_ticker, ticker_count = tickers.most_common(1)[0]
    
    if ticker_count / len(decided) >= 0.2:
        return f"A high-activity week anchored by {top_ticker} conviction."
    if top_count / len(decided) >= 0.3:
        return f"A high-activity week anchored by {top_strat} setups."
        
    return "A high-activity week testing multiple setups."


def _standout_phrase(winners: list) -> str:
    """Name the top winner with editorial flair."""
    if not winners:
        return ""
    best = winners[0]
    return f"The winners were led by {_pretty_label(best)};"


def _strategy_breakdown(decided: list) -> str:
    """Group trades by strategy and summarize."""
    from collections import Counter
    strategies = Counter(p.kind for p in decided if p.kind)
    if len(strategies) < 2:
        return ""
    parts = [f"{count} {strat}" for strat, count in strategies.most_common(5)]
    return f"Strategy mix: {', '.join(parts)}."


def _ticker_concentration(decided: list) -> str:
    """Note if one ticker dominated the week."""
    from collections import Counter
    tickers = Counter(p.symbol for p in decided)
    if not tickers:
        return ""
    top_ticker, top_count = tickers.most_common(1)[0]
    if top_count >= 4 and top_count / len(decided) >= 0.15:
        wins = sum(1 for p in decided if p.symbol == top_ticker and p.realized_pl and p.realized_pl > 0)
        losses = top_count - wins
        return (f"{top_ticker} was the most-traded name this week ({top_count} closes, "
                f"{wins}W/{losses}L) — a conviction play worth watching.")
    unique = len(tickers)
    if unique >= 8:
        return f"Broad exposure across {unique} different names this week."
    return ""



def _pretty_label(p: ClosedPosition) -> str:
    """Readable instrument label for prose: ``SNDK $1,405 Call`` / ``GOOG``."""
    if p.asset == "option" and p.strike and p.option_type:
        return f"{p.symbol} {_strike_money(p.strike)} {p.option_type}"
    return p.symbol


# --------------------------------------------------------------------------- #
# Weekly journal — render + upsert
# --------------------------------------------------------------------------- #

_JOURNAL_ARRAY_OPEN = "export const weeklyJournals: WeeklyJournal[] = ["


def render_journal_entry(entry: dict) -> str:
    """Render one journal entry as a TS object literal (2-space array indent).

    String values go through ``json.dumps`` so apostrophes/quotes in the draft
    prose can never break the file.
    """
    def s(key: str) -> str:
        return _ts_str(entry[key])

    lines = ["  {"]
    lines.append(f"    slug: {s('slug')},")
    lines.append(f"    title: {s('title')},")
    lines.append(f"    weekOf: {s('weekOf')},")
    lines.append(f"    startDate: {s('startDate')},")
    lines.append(f"    endDate: {s('endDate')},")
    lines.append(f"    summary: {s('summary')},")
    lines.append("    body: [")
    for para in entry["body"]:
        lines.append(f"      {_ts_str(para)},")
    lines.append("    ],")
    lines.append(f"    trades: {entry['trades']},")
    lines.append(f"    wins: {entry['wins']},")
    lines.append(f"    losses: {entry['losses']},")
    lines.append(f"    winRatePct: {entry['winRatePct']},")
    lines.append("    highlights: [")
    for h in entry["highlights"]:
        lines.append(f"      {_render_highlight(h)},")
    lines.append("    ],")
    lines.append(f"    published: {'true' if entry['published'] else 'false'},")
    lines.append("  },")
    return "\n".join(lines)


def _render_highlight(h: dict) -> str:
    parts = [
        f"ticker: {_ts_str(h['ticker'])}",
        f"asset: {_ts_str(h['asset'])}",
        f"strategy: {_ts_str(h['strategy'])}",
    ]
    if h.get("contract"):
        parts.append(f"contract: {_ts_str(h['contract'])}")
    parts.append(f"direction: {_ts_str(h['direction'])}")
    parts.append(f"returnPct: {h['returnPct'] if h['returnPct'] is not None else 'null'}")
    parts.append(f"note: {_ts_str(h.get('note', ''))}")
    return "{ " + ", ".join(parts) + " }"


def upsert_journal_entry(entry: dict, path: Path) -> bool:
    """Insert ``entry`` at the top of the ``weeklyJournals`` array in ``path``.

    Returns True if inserted, False if an entry with the same slug already
    exists (idempotent — a re-run for the same week is a no-op). Newest-first
    insert; the site sorts by date anyway.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")

    if re.search(rf"slug:\s*['\"]{re.escape(entry['slug'])}['\"]", text):
        return False
    if _JOURNAL_ARRAY_OPEN not in text:
        raise ValueError(f"Could not find the journals array opener in {path}")

    block = render_journal_entry(entry) + "\n"
    text = text.replace(_JOURNAL_ARRAY_OPEN + "\n", _JOURNAL_ARRAY_OPEN + "\n" + block, 1)
    path.write_text(text, encoding="utf-8")
    return True


# Machine-owned fields — the ones the sheet is the source of truth for. Prose
# (title/summary/body/highlights) and `published` are the human's and never
# refreshed. Order doesn't matter; each is replaced independently.
_STAT_FIELDS = ("weekOf", "startDate", "endDate", "trades", "wins", "losses", "winRatePct")


def refresh_journal_stats(entry: dict, path: Path) -> bool:
    """Update only the stat fields on an already-present week's entry in ``path``.

    A mid-week re-run refreshes the tiles (trades/win-rate/dates) as more trades
    close, while the reviewer's narrative and curated highlights survive. Returns
    True if the file changed, False if the slug isn't present *or* the stats
    already match (so a self-updating PR doesn't churn on a no-op run).

    CAVEAT: only the structured fields refresh — numbers the writer hard-codes
    into prose (e.g. "70 positions closed" in ``body``) are NOT rewritten. Keep
    prose qualitative and let the stat tiles carry the exact figures.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    span = _find_entry_block(text, entry["slug"])
    if span is None:
        return False

    start, end = span
    original = text[start:end]
    updated = original
    for field in _STAT_FIELDS:
        updated = _replace_stat_field(updated, field, entry[field])
    if updated == original:
        return False

    path.write_text(text[:start] + updated + text[end:], encoding="utf-8")
    return True


def _find_entry_block(text: str, slug: str) -> Optional[tuple[int, int]]:
    """Char span ``[open_brace, close_brace+1)`` of the object literal that holds
    ``slug: '<slug>'`` — brace-matched so nested ``highlights`` objects are
    included. ``None`` if the slug isn't in the file."""
    m = re.search(rf"slug:\s*['\"]{re.escape(slug)}['\"]", text)
    if not m:
        return None

    # Walk back to the '{' that opens this entry (skip balanced inner braces).
    depth, open_idx, j = 0, None, m.start()
    while j >= 0:
        c = text[j]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                open_idx = j
                break
            depth -= 1
        j -= 1
    if open_idx is None:
        return None

    # Walk forward to its matching close.
    depth = 0
    for k in range(open_idx, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return open_idx, k + 1
    return None


def _replace_stat_field(block: str, field: str, value) -> str:
    """Replace ``field: <old>`` with ``field: <value>`` in one entry block,
    preserving the existing quote style for string values."""
    if isinstance(value, int):
        return re.sub(
            rf"({re.escape(field)}:\s*)-?\d+",
            lambda m: f"{m.group(1)}{value}",
            block, count=1,
        )
    return re.sub(
        rf"({re.escape(field)}:\s*)(['\"]).*?\2",
        lambda m: f"{m.group(1)}{m.group(2)}{value}{m.group(2)}",
        block, count=1,
    )


# --------------------------------------------------------------------------- #
# Drift check — did the story go stale since the draft was written?
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class JournalDrift:
    """How far an existing week's entry has drifted from the live sheet.

    Refresh-in-place fixes the stat tiles, but the prose + curated highlights are
    a snapshot. When more trades close after the draft, the narrative may no
    longer fit — this flags it so the reviewer revises before merging (we never
    auto-overwrite curation)."""
    prev_trades: int
    new_trades: int
    top_winner: str   # current standout from live data, "" if none
    top_loser: str

    @property
    def grew(self) -> bool:
        return self.new_trades > self.prev_trades

    @property
    def added(self) -> int:
        return max(0, self.new_trades - self.prev_trades)


def assess_journal_drift(entry: dict, path: Path) -> Optional[JournalDrift]:
    """Compare the live entry against the one already in ``path`` (read BEFORE a
    refresh writes). ``None`` when the slug isn't present yet (a fresh insert has
    nothing to drift from)."""
    path = Path(path)
    if not path.exists():
        return None
    span = _find_entry_block(path.read_text(encoding="utf-8"), entry["slug"])
    if span is None:
        return None
    block = path.read_text(encoding="utf-8")[span[0]:span[1]]
    m = re.search(r"\btrades:\s*(\d+)", block)
    return JournalDrift(
        prev_trades=int(m.group(1)) if m else 0,
        new_trades=int(entry.get("trades", 0)),
        top_winner=_top_highlight(entry, "win"),
        top_loser=_top_highlight(entry, "loss"),
    )


def _top_highlight(entry: dict, direction: str) -> str:
    """Best highlight of ``direction`` from the freshly-built entry, e.g.
    ``SNDK $1,405 Call (+230.6%)`` — the standout the reviewer should check is
    still represented in the prose."""
    for h in entry.get("highlights", []):
        if h.get("direction") == direction:
            label = h["ticker"] + (f" {h['contract']}" if h.get("contract") else "")
            pct = h.get("returnPct")
            return f"{label} ({pct:+.1f}%)" if pct is not None else label
    return ""


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

def _cell(row: list[Any], i: int) -> Any:
    return row[i] if i < len(row) else ""


def _dec(val: Any) -> Optional[Decimal]:
    """Coerce a (possibly currency-formatted) sheet cell to Decimal; '' -> None."""
    if val is None or val == "":
        return None
    s = str(val).strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("$", "").replace("−", "-").strip()
    if not s:
        return None
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return -d if neg else d


def _is_malformed(underlying: str) -> bool:
    """A "/" in an underlying means a combo/spread symbol leaked into the Stock
    or Ticker column (source-data glitch) — never a real equity ticker."""
    return "/" in underlying


def _norm_type(t: str) -> str:
    t = t.strip().lower()
    if t.startswith("c"):
        return "Call"
    if t.startswith("p"):
        return "Put"
    return t.title()


def _num(d: Decimal) -> str:
    """Decimal → JS number literal: int when integral, else plain decimal."""
    d = Decimal(d)
    if d == d.to_integral_value():
        return str(int(d))
    return format(d.normalize(), "f")


def _strike_money(strike: str) -> str:
    """A strike display like ``$1,405`` from the stored ``$1,405.00`` / ``1405``."""
    d = _dec(strike)
    if d is None:
        return f"${str(strike).strip()}"
    if d == d.to_integral_value():
        return f"${int(d):,}"
    return f"${d.normalize():,f}"


def _round_pct(pct: Optional[Decimal]):
    if pct is None:
        return None
    return round(float(pct), 1)


def _ret_str(p: ClosedPosition) -> str:
    return f"{p.return_pct:+.1f}%" if p.return_pct is not None else "closed"


def _ts_str(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _week_span(recent: list[ClosedPosition], today: date, window_days: int) -> tuple[date, date]:
    dates = [d for d in (_parse_iso(p.close_date) for p in recent) if d is not None]
    if not dates:
        return today - timedelta(days=window_days), today
    return min(dates), max(dates)


def _parse_iso(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(str(s).strip()[:10])
    except (ValueError, TypeError):
        return None


def _format_span(start: date, end: date) -> str:
    """e.g. ``Aug 10–14, 2026`` or ``Aug 28–Sep 3, 2026``."""
    if start.month == end.month and start.year == end.year:
        return f"{start:%b} {start.day}–{end.day}, {end.year}"
    if start.year == end.year:
        return f"{start:%b} {start.day}–{end:%b} {end.day}, {end.year}"
    return f"{start:%b} {start.day}, {start.year}–{end:%b} {end.day}, {end.year}"
