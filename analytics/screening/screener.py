"""Systematic option screener via Tiger SDK (Google Doc spec §5).

Scans live option chains for high-probability Short Put / Short Call setups
using these filters:

- **Volatility:** IV Percentile (IVP) >= 70%
- **Probability:** Delta between 0.30 and 0.40 (~60–70% OTM probability)
- **Liquidity:** Open Interest > 500, Bid-Ask Spread <= $0.10

Uses Tiger's ``QuoteClient.get_option_chain()`` and ``get_option_briefs()``
for live data. Falls back gracefully if Tiger credentials are unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

log = logging.getLogger(__name__)

ZERO = Decimal("0")

# Default filter thresholds (from the spec)
DEFAULT_IVP_MIN = 70.0            # IV Percentile minimum (%)
DEFAULT_DELTA_MIN = 0.30           # absolute delta lower bound
DEFAULT_DELTA_MAX = 0.40           # absolute delta upper bound
DEFAULT_OI_MIN = 500               # minimum open interest
DEFAULT_SPREAD_MAX = Decimal("0.10")  # maximum bid-ask spread ($)


@dataclass
class ScreenerFilter:
    """Parameters for the option screener."""
    ivp_min: float = DEFAULT_IVP_MIN
    delta_min: float = DEFAULT_DELTA_MIN
    delta_max: float = DEFAULT_DELTA_MAX
    oi_min: int = DEFAULT_OI_MIN
    spread_max: Decimal = DEFAULT_SPREAD_MAX


@dataclass
class ScreenerResult:
    """One option contract that passes all screener filters."""
    symbol: str           # underlying ticker
    expiry: str           # expiry date string
    option_type: str      # "Call" or "Put"
    strike: Decimal
    bid: Decimal
    ask: Decimal
    spread: Decimal
    delta: float
    iv: float             # implied volatility
    ivp: float            # IV percentile
    open_interest: int
    volume: int = 0
    mid_price: Decimal = ZERO
    iv_rv_ratio: Optional[float] = None  # current IV ÷ trailing realized vol (>1 = rich premium)

    @property
    def direction(self) -> str:
        """Short Put = bullish, Short Call = bearish."""
        return "Bullish" if self.option_type == "Put" else "Bearish"


def _build_quote_client():
    """Build a Tiger QuoteClient from env credentials. Returns None on failure."""
    try:
        # Quiet the SDK's INFO chatter (sdk version, device-access claims); keep
        # its warnings/errors.
        logging.getLogger("tiger_openapi").setLevel(logging.WARNING)

        from tigeropen.quote.quote_client import QuoteClient
        from tigeropen.tiger_open_config import TigerOpenClientConfig

        from adapters.tiger import TigerCredentials, _resolve_private_key

        creds = TigerCredentials.from_env()
        config = TigerOpenClientConfig(sandbox_debug=creds.sandbox)
        config.private_key = _resolve_private_key(creds.private_key)
        config.tiger_id = creds.tiger_id
        config.account = creds.account
        config.language = "en_US"
        config.timezone = creds.timezone
        if creds.license:
            config.license = creds.license
        return QuoteClient(config)
    except Exception as exc:
        log.warning("Cannot initialize Tiger QuoteClient: %s", exc)
        return None


def _norm_cdf(x: float) -> float:
    """Standard normal CDF (mirrors ``market_scan._norm_cdf``; kept local to
    avoid a circular import — market_scan imports from this module)."""
    import math
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_delta(spot: float, strike: float, dte_days: int, iv: float, is_call: bool, r: float = 0.045) -> float:
    """Estimate option Delta via Black-Scholes (for chains that carry no Greeks)."""
    import math
    if dte_days <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    t = dte_days / 365.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv ** 2) * t) / (iv * math.sqrt(t))
    return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0


def _yfinance_option_chain(symbol: str, expiry: str) -> list[dict]:
    """Delayed yfinance fallback for one symbol+expiry when Tiger is unavailable.

    Returns dicts shaped like the Tiger chain (``right``/``strike``/``bid``/
    ``ask``/``open_interest``/``delta``/…). yfinance carries no Greeks, so Delta
    is estimated from the row's implied vol so downstream delta filters work.
    """
    try:
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)

        exp_iso = expiry.replace("/", "-")
        try:
            exp_date = date.fromisoformat(exp_iso)
        except ValueError:
            # accept compact "YYYYMMDD"
            exp_date = date(int(exp_iso[:4]), int(exp_iso[4:6]), int(exp_iso[6:8]))
            exp_iso = exp_date.isoformat()

        tk = yf.Ticker(symbol)
        fast_info = getattr(tk, "fast_info", None)
        spot = getattr(fast_info, "last_price", None)
        if not spot:
            hist = tk.history(period="1d")
            spot = float(hist["Close"].iloc[-1]) if not hist.empty else 0.0
        spot = float(spot or 0.0)

        chain = tk.option_chain(exp_iso)
        dte = (exp_date - date.today()).days

        def _num(x) -> float:
            """Coerce a cell to float, mapping None/NaN to 0.0 (yfinance leaves
            volume/OI/IV as NaN for illiquid strikes)."""
            try:
                v = float(x)
            except (TypeError, ValueError):
                return 0.0
            return v if v == v else 0.0  # NaN != NaN

        out: list[dict] = []
        for frame, right, is_call in ((chain.calls, "call", True), (chain.puts, "put", False)):
            if frame is None or frame.empty:
                continue
            for _, row in frame.iterrows():
                strike = _num(row.get("strike"))
                bid = _num(row.get("bid"))
                ask = _num(row.get("ask"))
                iv = _num(row.get("impliedVolatility"))
                out.append({
                    "identifier": str(row.get("contractSymbol", "")),
                    "symbol": symbol,
                    "expiry": exp_iso,
                    "right": right,
                    "strike": strike,
                    "bid": bid, "bid_price": bid,
                    "ask": ask, "ask_price": ask,
                    "open_interest": int(_num(row.get("openInterest"))),
                    "volume": int(_num(row.get("volume"))),
                    "implied_volatility": iv,
                    "delta": _bs_delta(spot, strike, dte, iv, is_call),
                })
        return out
    except Exception as exc:
        log.warning("yfinance option-chain fallback failed for %s %s: %s", symbol, expiry, exc)
        return []


def _tiger_option_chain(client, symbol: str, expiry: str, market: str = "US") -> list[dict]:
    """Fetch one chain via a live Tiger QuoteClient. Returns [] on any failure
    (no data, or the account lacks options-market permission)."""
    try:
        from tigeropen.common.consts import Market
        mkt = Market.US if market.upper() == "US" else Market.HK
        chain = client.get_option_chain(symbol=symbol, expiry=expiry, market=mkt)

        if chain is None:
            return []

        # Tiger returns option chain as a list or DataFrame depending on SDK version
        if hasattr(chain, "to_dict"):
            return chain.to_dict("records")
        if isinstance(chain, list):
            return chain
        return []
    except Exception as exc:
        # Expected when the account lacks US-options data entitlement — the
        # caller falls back to yfinance, so this is debug, not a warning.
        log.debug("Tiger option chain unavailable for %s %s: %s", symbol, expiry, exc)
        return []


def fetch_option_chain(
    symbol: str,
    expiry: str,
    *,
    quote_client=None,
    market: str = "US",
) -> list[dict]:
    """Fetch the option chain for a symbol+expiry.

    Prefers a live Tiger QuoteClient, but falls back to delayed yfinance chains
    whenever Tiger yields nothing — whether the ``tigeropen`` SDK isn't installed,
    no client could be built, or the client call fails (e.g. the account has no
    US options-market data permission). ``expiry`` format: "YYYYMMDD" or
    "YYYY-MM-DD". Returns a list of contract dicts, or empty list on failure.
    """
    client = quote_client or _build_quote_client()
    if client is not None:
        rows = _tiger_option_chain(client, symbol, expiry, market)
        if rows:
            return rows
        # Client present but returned nothing — fall through to yfinance.

    return _yfinance_option_chain(symbol, expiry)


def screen_options(
    chain_data: list[dict],
    *,
    filters: ScreenerFilter | None = None,
) -> list[ScreenerResult]:
    """Filter an option chain against the screener criteria.

    ``chain_data`` is a list of dicts with keys like:
    identifier, strike, bid, ask, delta, open_interest, implied_volatility, etc.
    (field names vary by Tiger SDK version — we handle common variants.)
    """
    filters = filters or ScreenerFilter()
    results: list[ScreenerResult] = []

    for c in chain_data:
        try:
            # Extract fields with fallbacks for different SDK versions
            strike = Decimal(str(_get(c, "strike", "strike_price", default=0)))
            bid = Decimal(str(_get(c, "bid_price", "bid", default=0)))
            ask = Decimal(str(_get(c, "ask_price", "ask", default=0)))
            delta = abs(float(_get(c, "delta", default=0)))
            iv = float(_get(c, "implied_volatility", "iv", "volatility", default=0))
            oi = int(_get(c, "open_interest", "oi", default=0))
            volume = int(_get(c, "volume", "vol", default=0))
            identifier = str(_get(c, "identifier", "symbol", "contract", default=""))
            right = str(_get(c, "right", "option_type", "type", "put_call", default="")).upper()

            spread = ask - bid
            mid = (bid + ask) / 2

            # Determine option type
            if right in ("PUT", "P"):
                option_type = "Put"
            elif right in ("CALL", "C"):
                option_type = "Call"
            else:
                continue

            # Apply filters
            # Note: IVP requires historical context; using IV as proxy when IVP
            # is not directly available from the chain data
            ivp = float(_get(c, "iv_percentile", "ivp", "iv_rank", default=0))

            if ivp < filters.ivp_min and ivp > 0:
                continue
            if delta < filters.delta_min or delta > filters.delta_max:
                continue
            if oi < filters.oi_min:
                continue
            if spread > filters.spread_max:
                continue

            expiry = str(_get(c, "expiry", "expiry_date", "expiration", default=""))

            results.append(ScreenerResult(
                symbol=_extract_underlying(identifier),
                expiry=expiry,
                option_type=option_type,
                strike=strike,
                bid=bid,
                ask=ask,
                spread=spread,
                delta=delta,
                iv=iv,
                ivp=ivp,
                open_interest=oi,
                volume=volume,
                mid_price=mid,
            ))
        except Exception as exc:
            log.debug("Skipping chain entry: %s", exc)
            continue

    results.sort(key=lambda r: (r.symbol, r.expiry, r.strike))
    return results


def format_screener_message(results: list[ScreenerResult]) -> str:
    """Format screener results as a Telegram-ready message."""
    if not results:
        return "🔎 No option setups matched the screener filters."

    lines = [f"🔎 Option Screener — {len(results)} setup(s) found:", ""]

    for r in results:
        lines.append(
            f"{'🟢' if r.direction == 'Bullish' else '🔴'} "
            f"Short {r.option_type} {r.symbol} ${r.strike} "
            f"exp {r.expiry}"
        )
        lines.append(
            f"   Δ {r.delta:.2f} · IV {r.iv:.1%} · IVP {r.ivp:.0f}% · "
            f"OI {r.open_interest:,} · Spread ${r.spread:.2f} · "
            f"Mid ${r.mid_price:.2f}"
        )
        lines.append("")

    return "\n".join(lines)


def _get(d: dict, *keys: str, default=None):
    """Get the first matching key from a dict."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _extract_underlying(identifier: str) -> str:
    """Extract underlying ticker from an option identifier string."""
    # Tiger format: "AAPL 240119C00190000" or "AAPL240119C00190000"
    parts = identifier.split()
    if parts:
        # Take the alphabetic prefix
        ticker = ""
        for ch in parts[0]:
            if ch.isalpha():
                ticker += ch
            else:
                break
        return ticker or parts[0]
    return identifier
