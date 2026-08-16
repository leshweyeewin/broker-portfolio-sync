"""Lemon8 trade-log table images (step 10 — BUILD_SPEC.md §11b).

The weekly post's image carousel: a trade-log table — one row per closed trade,
both stocks and options — split across 3:4 portrait pages.

Privacy Constraint (Load-bearing):
- Renders ONLY Date, Instrument, win/loss marker, and return % (where derivable).
- Absolute dollar amounts and portfolio size are NEVER shown.
"""

from __future__ import annotations

from datetime import date
from xml.sax.saxutils import escape as _xml_escape

from lemon8.reader import ClosedPosition

# Lemon8 image cards are 3:4 portrait. 1080×1440 is the standard upload size.
TABLE_W = 1080
TABLE_H = 1440
TABLE_ROWS_PER_PAGE = 25   # what fits comfortably at phone-readable size


# --------------------------------------------------------------------------- #
# Trade-log table pages (the weekly post's image carousel)
# --------------------------------------------------------------------------- #
#
# One row per closed execution, both stocks and options together, split across
# 3:4 portrait pages. Privacy-safe: Date | Instrument | Result | Return % — no
# dollar amounts. A dot (green/red/grey) marks win/loss/undetermined, and the
# return % is shown only where it can be derived (see reader._return_pct).

_COL_DATE_X = 56
_COL_LABEL_X = 190
_COL_DOT_CX = 812
_COL_PCT_X = 860        # left edge of the (right-ish) % column
_ROW_TOP = 300          # y of the first data row baseline
_ROW_H = 42

_WIN = "#10B981"
_LOSS = "#EF4444"
_FLAT = "#6B7280"


def _result_color(pos: ClosedPosition) -> str:
    return _WIN if pos.is_win is True else (_LOSS if pos.is_win is False else _FLAT)


def _pct_cell(pos: ClosedPosition) -> tuple[str, str]:
    """(text, color) for the return-% column. Em dash when not derivable."""
    if pos.return_pct is None:
        return "—", _FLAT
    return f"{pos.return_pct:+.1f}%", (_WIN if pos.return_pct >= 0 else _LOSS)


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def render_trade_table_pages(
    positions: list[ClosedPosition],
    week_ending: date,
    *,
    rows_per_page: int = TABLE_ROWS_PER_PAGE,
) -> list[str]:
    """Render the week's closed trades as one or more 3:4 portrait table SVGs.

    Most-recent first; both asset classes in a single table. Returns one SVG
    string per page (always at least one, even with no trades).
    """
    rows = sorted(positions, key=lambda p: (p.close_date, p.label), reverse=True)
    pages = [rows[i : i + rows_per_page] for i in range(0, len(rows), rows_per_page)] or [[]]
    total = len(pages)
    return [
        _render_table_page(page, page_no=i + 1, page_total=total,
                           week_ending=week_ending, trade_total=len(rows))
        for i, page in enumerate(pages)
    ]


def _render_table_page(
    page_rows: list[ClosedPosition],
    *,
    page_no: int,
    page_total: int,
    week_ending: date,
    trade_total: int,
) -> str:
    subtitle = (
        f"week ending {week_ending:%d %b %Y} · {trade_total} closed · "
        f"page {page_no}/{page_total}"
    )
    parts = [
        f'<svg width="{TABLE_W}" height="{TABLE_H}" viewBox="0 0 {TABLE_W} {TABLE_H}" '
        'xmlns="http://www.w3.org/2000/svg">',
        f'<rect width="{TABLE_W}" height="{TABLE_H}" fill="#1F2937"/>',
        f'<rect x="0" y="0" width="{TABLE_W}" height="12" fill="{_WIN}"/>',
        f'<text x="{_COL_DATE_X}" y="110" font-family="sans-serif" font-weight="bold" '
        f'font-size="46" fill="#FFFFFF">Weekly Trades</text>',
        f'<text x="{_COL_DATE_X}" y="158" font-family="sans-serif" font-size="26" '
        f'fill="#9CA3AF">{_xml_escape(subtitle)}</text>',
        # column header + rule
        f'<text x="{_COL_DATE_X}" y="238" font-family="sans-serif" font-size="24" '
        f'fill="#9CA3AF">DATE</text>',
        f'<text x="{_COL_LABEL_X}" y="238" font-family="sans-serif" font-size="24" '
        f'fill="#9CA3AF">INSTRUMENT</text>',
        f'<text x="{_COL_PCT_X}" y="238" font-family="sans-serif" font-size="24" '
        f'fill="#9CA3AF">RETURN</text>',
        f'<line x1="{_COL_DATE_X}" y1="256" x2="{TABLE_W - _COL_DATE_X}" y2="256" '
        'stroke="#374151" stroke-width="2"/>',
    ]

    for i, pos in enumerate(page_rows):
        y = _ROW_TOP + i * _ROW_H
        if i % 2 == 1:  # subtle zebra striping
            parts.append(
                f'<rect x="{_COL_DATE_X - 16}" y="{y - 30}" width="{TABLE_W - 2 * (_COL_DATE_X - 16)}" '
                f'height="{_ROW_H}" rx="6" fill="#FFFFFF" fill-opacity="0.03"/>'
            )
        date_txt = _xml_escape(pos.close_date[5:] if len(pos.close_date) >= 10 else pos.close_date)
        label_txt = _xml_escape(_trunc(pos.label, 34))
        pct_txt, pct_color = _pct_cell(pos)
        parts.extend([
            f'<text x="{_COL_DATE_X}" y="{y}" font-family="sans-serif" font-size="28" '
            f'fill="#9CA3AF">{date_txt}</text>',
            f'<text x="{_COL_LABEL_X}" y="{y}" font-family="sans-serif" font-size="28" '
            f'fill="#F3F4F6">{label_txt}</text>',
            f'<circle cx="{_COL_DOT_CX}" cy="{y - 9}" r="9" fill="{_result_color(pos)}"/>',
            f'<text x="{_COL_PCT_X}" y="{y}" font-family="sans-serif" font-weight="bold" '
            f'font-size="28" fill="{pct_color}">{_xml_escape(pct_txt)}</text>',
        ])

    parts.append(
        f'<text x="{_COL_DATE_X}" y="{TABLE_H - 40}" font-family="sans-serif" font-size="22" '
        f'fill="#6B7280">● win  ● loss  ● undetermined · % shown only where derivable</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts)
