"""On-demand Telegram quote bot (long-polling).

The daily job (``run.py``) only *pushes* alerts. This adds the *inbound* path: a
small always-on process that long-polls Telegram ``getUpdates`` and answers a
``/quote TICKER`` (or bare ``TICKER``) with a **quant** quick-take — price,
trend/RSI/ATR, 52-week position, and the next earnings date with its
straddle-implied expected move.

Design (mirrors ``alerting/notify.py``):
* No third-party dependency — plain ``urllib`` for the HTTP GET, and replies go
  back through the existing ``send_telegram``.
* Everything the bot *says* is a pure function (``format_quote``) so it can be
  unit-tested offline; the network fetch (``build_quote``) fails soft.
* The poll loop takes an injectable ``transport`` and ``reply`` so tests drive it
  without touching Telegram or yfinance.

Run it with::

    python -m alerting.bot

Optionally register the command menu once via Telegram's ``setMyCommands`` so
``/quote`` autocompletes in the app (not required for the bot to work).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import date
from typing import Callable, Optional
from urllib.parse import urlencode

from alerting.notify import send_telegram
from analytics.screening.market_scan import get_upcoming_earnings
from analytics.screening.swing import SwingSetup, scan_swing_setups
from config.settings import get_telegram_bot_token

log = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"
_COMMANDS = ("quote", "q", "quicktake", "qt")

# Options commands: each alias maps to a builder key resolved in ``run_bot``.
_OPTION_COMMANDS = {
    "directional": "directional", "dir": "directional",
    "options": "directional", "opt": "directional",
    "midweek": "midweek", "mid": "midweek", "mw": "midweek",
}

# Argument-free commands: scan the watchlist / current positions (no ticker).
_NULLARY_COMMANDS = {
    "spreads": "spreads", "spread": "spreads", "iv": "spreads",
    "wheel": "wheel", "income": "wheel",
}

_USAGE = (
    "📊 Send me a ticker for a quick-take.\n"
    "Try: /quote NVDA  (or just: NVDA)\n"
    "You get price, trend/RSI/ATR, 52w position, and the next earnings date "
    "with its expected move.\n\n"
    "📈 Options (read-only plans, no orders):\n"
    "/directional NVDA — bull-call / long / CSP / covered-call / PMCC board\n"
    "/midweek SPY — short-dated (0–5 DTE) expiry templates\n"
    "/spreads — earnings credit-spread scan over your watchlist (no ticker)\n"
    "/wheel — CSP / covered-call / PMCC from your current positions (no ticker)"
)

# A transport takes (url, timeout seconds) and returns the parsed JSON payload.
Transport = Callable[[str, int], dict]
# A reply takes (chat_id, text) and delivers it.
Reply = Callable[[str, str], None]


# --------------------------------------------------------------------------- #
# Parsing + formatting (pure — unit-tested offline)
# --------------------------------------------------------------------------- #

def parse_ticker(text: str) -> Optional[str]:
    """Extract a ticker from a message, or None if it isn't a quote request.

    Accepts ``/quote NVDA``, ``/q nvda``, ``/quote@YourBot TSLA``, or a bare
    single token ``NVDA``. Returns the upper-cased symbol (1–5 letters) or None.
    """
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("/"):
        parts = t.split()
        cmd = parts[0].lstrip("/").split("@")[0].lower()  # strip @botname suffix
        if cmd not in _COMMANDS or len(parts) < 2:
            return None
        cand = parts[1]
    else:
        parts = t.split()
        if len(parts) != 1:  # only a lone word is treated as a bare ticker
            return None
        cand = parts[0]
    cand = cand.upper()
    return cand if (cand.isalpha() and 1 <= len(cand) <= 5) else None


def parse_options_command(text: str):
    """Extract ``(builder_key, ticker)`` for an options command, or None.

    Accepts ``/directional NVDA``, ``/dir nvda``, ``/midweek@Bot spy`` etc.
    ``builder_key`` is one of the values in ``_OPTION_COMMANDS`` (e.g.
    ``"directional"``) that ``run_bot`` resolves against its builder registry.
    """
    t = (text or "").strip()
    if not t.startswith("/"):
        return None
    parts = t.split()
    cmd = parts[0].lstrip("/").split("@")[0].lower()  # strip @botname suffix
    key = _OPTION_COMMANDS.get(cmd)
    if key is None or len(parts) < 2:
        return None
    cand = parts[1].upper()
    return (key, cand) if (cand.isalpha() and 1 <= len(cand) <= 5) else None


def parse_nullary_command(text: str) -> Optional[str]:
    """Extract the builder key for an argument-free command (``/spreads``, ``/wheel``).

    Returns the ``_NULLARY_COMMANDS`` value (e.g. ``"spreads"``) or None. Any
    trailing tokens are ignored — these commands take no ticker.
    """
    t = (text or "").strip()
    if not t.startswith("/"):
        return None
    cmd = t.split()[0].lstrip("/").split("@")[0].lower()  # strip @botname suffix
    return _NULLARY_COMMANDS.get(cmd)


def is_help(text: str) -> bool:
    """True for /start or /help (so the bot can reply with usage)."""
    return (text or "").strip().lower().split("@")[0] in ("/start", "/help")


def format_quote(
    setup: SwingSetup,
    *,
    earnings_date: Optional[date] = None,
    days_left: Optional[int] = None,
    expected_move_pct: Optional[float] = None,
) -> str:
    """Render a quant quick-take for one ticker (Telegram plain text)."""
    price = f"${setup.price:,.2f}" if setup.price else "n/a"
    lines = [f"📊 {setup.ticker} [{setup.theme}] — {price}"]

    setup_line = f"Setup: {setup.setup}"
    if setup.note:
        setup_line += f" · {setup.note}"
    lines.append(setup_line)

    tech: list[str] = []
    if setup.rsi14 is not None:
        tech.append(f"RSI {setup.rsi14:.0f}")
    if setup.pct_vs_ma20 is not None:
        tech.append(f"vs20 {setup.pct_vs_ma20:+.1f}%")
    if setup.pct_vs_ma50 is not None:
        tech.append(f"vs50 {setup.pct_vs_ma50:+.1f}%")
    if setup.atr_pct is not None:
        tech.append(f"ATR {setup.atr_pct:.1f}%/day")
    if tech:
        lines.append("Trend: " + " · ".join(tech))

    if setup.pct_below_52w_high is not None:
        lines.append(f"52w high: -{setup.pct_below_52w_high:.1f}% off")

    if earnings_date is not None:
        dte = f" (in {days_left}d)" if days_left is not None else ""
        em = f" · ±{expected_move_pct:.1f}% exp move" if expected_move_pct is not None else ""
        lines.append(f"Earnings: {earnings_date:%a %d %b}{dte}{em}")
    elif days_left is not None:
        lines.append(f"Earnings: in {days_left}d")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Data fetch (network; fails soft)
# --------------------------------------------------------------------------- #

def build_quote(ticker: str, *, today: Optional[date] = None) -> str:
    """Build the quick-take message for ``ticker`` from yfinance-backed analytics."""
    setups = scan_swing_setups([ticker], today=today)
    setup = setups[0] if setups else None
    if setup is None or setup.setup == "No-data":
        return f"⚠️ No market data for {ticker} right now — try again shortly."

    e0 = None
    try:
        events = get_upcoming_earnings([ticker], today=today, days_ahead=90, with_expected_move=True)
        e0 = events[0] if events else None
    except Exception as exc:
        log.debug("Earnings/expected-move lookup failed for %s: %s", ticker, exc)

    return format_quote(
        setup,
        earnings_date=e0.earnings_date if e0 else None,
        days_left=e0.days_left if e0 else setup.days_to_earnings,
        expected_move_pct=e0.expected_move_pct if e0 else None,
    )


# --------------------------------------------------------------------------- #
# Options builders (reuse the existing CLIs in-process by capturing stdout)
# --------------------------------------------------------------------------- #

def _capture_cli(cli_main: Callable[[list], int], *args: str) -> str:
    """Run a CLI ``main([*args])`` and capture its stdout (``main([])`` if no args)."""
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli_main(list(args))
    return buf.getvalue()


def _trim_for_telegram(text: str, limit: int = 3500) -> str:
    """Strip and cap CLI output so it fits a single Telegram message (< 4096)."""
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n…(truncated)"
    return text


def build_directional(ticker: str, *, today: Optional[date] = None) -> str:
    """Directional/income options board for ``ticker`` (bull-call, long, CSP, CC, PMCC)."""
    from analytics.options.directional_builder import main as directional_main

    out = _trim_for_telegram(_capture_cli(directional_main, ticker))
    return out or f"⚠️ No directional setups for {ticker} right now."


def build_midweek(ticker: str, *, today: Optional[date] = None) -> str:
    """Short-dated (0–5 DTE) expiry planner templates for ``ticker``."""
    from analytics.options.mid_week_planner import main as midweek_main

    out = _trim_for_telegram(_capture_cli(midweek_main, ticker))
    return out or f"⚠️ No short-dated expiries for {ticker} right now."


# Builder registry resolved by ``parse_options_command`` keys. Injectable in tests.
_DEFAULT_OPTION_BUILDERS: dict[str, Callable[..., str]] = {
    "directional": build_directional,
    "midweek": build_midweek,
}


def build_spreads(*, today: Optional[date] = None) -> str:
    """Earnings-driven credit-spread scan over your watchlist (no ticker needed)."""
    from analytics.earnings.iv_crush import scan_iv_crush, format_message, earnings_universe

    universe = earnings_universe()
    if not universe:
        return "⚠️ No earnings watchlist configured yet."
    cands = scan_iv_crush(universe, today=today)
    if not cands:
        return "⚠️ No upcoming earnings in your watchlist horizon right now."
    return _trim_for_telegram(format_message(cands))


def build_wheel(*, today: Optional[date] = None) -> str:
    """Wheel / CSP / CC / PMCC scan from your current Sheet positions (no ticker needed)."""
    from analytics.options.income_workspace import main as wheel_main

    out = _trim_for_telegram(_capture_cli(wheel_main))
    return out or "⚠️ No wheel setups from your current positions right now."


# Nullary (argument-free) commands: scan your watchlist / positions, no ticker.
_DEFAULT_NULLARY_BUILDERS: dict[str, Callable[..., str]] = {
    "spreads": build_spreads,
    "wheel": build_wheel,
}


# --------------------------------------------------------------------------- #
# Telegram long-polling
# --------------------------------------------------------------------------- #

def _http_get_json(url: str, timeout: int) -> dict:
    """GET ``url`` and parse the JSON body (raises on network/parse error)."""
    req = urllib.request.Request(url, headers={"User-Agent": "broker-portfolio-sync/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_updates(offset: Optional[int], *, token: str, timeout: int, transport: Transport) -> dict:
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    url = f"{_API_BASE}/bot{token}/getUpdates?{urlencode(params)}"
    return transport(url, timeout + 5)


def run_bot(
    *,
    token: Optional[str] = None,
    poll_timeout: int = 25,
    transport: Optional[Transport] = None,
    reply: Optional[Reply] = None,
    quote_builder: Callable[..., str] = build_quote,
    option_builders: Optional[dict] = None,
    nullary_builders: Optional[dict] = None,
    today: Optional[date] = None,
    once: bool = False,
) -> None:
    """Long-poll Telegram and answer quote requests until interrupted.

    ``transport``/``reply``/``quote_builder``/``option_builders``/``nullary_builders``
    are injectable for tests; ``once`` processes a single ``getUpdates`` batch.
    """
    token = token or get_telegram_bot_token()
    transport = transport or _http_get_json
    reply = reply or (lambda chat_id, text: send_telegram(text, chat_id=chat_id))
    option_builders = option_builders or _DEFAULT_OPTION_BUILDERS
    nullary_builders = nullary_builders or _DEFAULT_NULLARY_BUILDERS

    offset: Optional[int] = None
    while True:
        try:
            payload = _get_updates(offset, token=token, timeout=poll_timeout, transport=transport)
        except Exception as exc:  # network hiccup — back off and keep polling
            log.warning("getUpdates failed: %s", exc)
            if once:
                break
            time.sleep(3)
            continue

        for upd in payload.get("result", []):
            offset = upd.get("update_id", 0) + 1
            msg = upd.get("message") or upd.get("channel_post") or {}
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "")
            if chat_id is None:
                continue

            ticker = parse_ticker(text)
            if ticker is not None:
                try:
                    out = quote_builder(ticker, today=today)
                except Exception as exc:
                    log.warning("quote build failed for %s: %s", ticker, exc)
                    out = f"⚠️ Couldn't build a quote for {ticker} right now."
                reply(str(chat_id), out)
                continue

            opt = parse_options_command(text)
            if opt is not None:
                key, tk = opt
                builder = option_builders.get(key)
                if builder is not None:
                    try:
                        out = builder(tk, today=today)
                    except Exception as exc:
                        log.warning("%s build failed for %s: %s", key, tk, exc)
                        out = f"⚠️ Couldn't build {key} for {tk} right now."
                    reply(str(chat_id), out)
                continue

            nkey = parse_nullary_command(text)
            if nkey is not None:
                builder = nullary_builders.get(nkey)
                if builder is not None:
                    try:
                        out = builder(today=today)
                    except Exception as exc:
                        log.warning("%s build failed: %s", nkey, exc)
                        out = f"⚠️ Couldn't build {nkey} right now."
                    reply(str(chat_id), out)
                continue

            if is_help(text):
                reply(str(chat_id), _USAGE)

        if once:
            break


def main(argv=None) -> int:
    import argparse
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    p = argparse.ArgumentParser(description="On-demand Telegram quote bot (long-polling).")
    p.add_argument("--once", action="store_true", help="Process one batch and exit (debugging).")
    p.add_argument("--poll-timeout", type=int, default=25, help="Long-poll seconds per getUpdates.")
    args = p.parse_args(argv)

    log.info("Quote bot starting (poll_timeout=%ds). Ctrl-C to stop.", args.poll_timeout)
    try:
        run_bot(poll_timeout=args.poll_timeout, once=args.once)
    except KeyboardInterrupt:
        log.info("Quote bot stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
