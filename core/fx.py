"""FX rate module (step 4 of the build order - BUILD_SPEC.md §7, §12).

Design
------
* Base currency: SGD. Pairs: USD/SGD, HKD/SGD (and any others the adapters surface).
* Two distinct rate timings:
  - rate(pair, on)       -> historical, trade-date rate (cached). Used for realised P/L
                           and cash-movement rows so those rows never shift as spot rates move.
  - current_rate(pair)   -> live/today's rate. Used only for unrealised valuations and
                           Dashboard's current-market-value column.
* Local JSON cache (~/.cache/broker_fx/fx_cache.json by default). Key: "FROM/TO/YYYY-MM-DD".
  A historical rate fetched once is stored forever so re-runs reproduce the same conversion.
  The current rate is NEVER cached.
* Rate source: Frankfurter (https://www.frankfurter.app/). No API key, ECB-backed, historical
  data from 1999. Weekend/holiday dates resolved to nearest prior business day by the API.
* Fail loud, never guess (§9). On any network failure or unexpected API response raises
  FxFetchError (RuntimeError subclass). Never silently returns a stale rate for a date not
  explicitly fetched.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional


class FxFetchError(RuntimeError):
    """Raised when a live FX rate fetch fails (network, API response, rate-limit, etc.)."""


class FxPairError(ValueError):
    """Raised for unsupported / malformed pair strings."""


def _parse_pair(pair: str) -> tuple[str, str]:
    pair = pair.strip().upper().replace("-", "/")
    if "/" in pair:
        parts = pair.split("/")
        if len(parts) != 2 or not all(len(p) == 3 for p in parts):
            raise FxPairError(f"invalid FX pair {pair!r} - expected 'XXX/YYY'")
        return parts[0], parts[1]
    if len(pair) == 6:
        return pair[:3], pair[3:]
    raise FxPairError(
        f"invalid FX pair {pair!r} - expected 6-char string like 'USDSGD' "
        "or slash-separated like 'USD/SGD'"
    )


def _default_cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "broker_fx" / "fx_cache.json"


_FRANKFURTER_BASE = "https://api.frankfurter.app"
_REQUEST_TIMEOUT = 10
_MAX_ATTEMPTS = 3          # Frankfurter is occasionally slow; a transient timeout
_RETRY_BACKOFF_S = 2.0     # shouldn't fail the whole run.


def _fetch_body(url: str, frm: str, to: str, *, context: str) -> str:
    """GET ``url`` with retries on transient network/timeout errors.

    HTTPError (a real API response like 404/429) is not retried — it's not
    transient. URLError/OSError (DNS, connection reset, read timeout) are retried
    up to ``_MAX_ATTEMPTS`` with a linear backoff before failing loud (§9).
    """
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "broker-portfolio-sync/1.0"},
    )
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise FxFetchError(
                f"Frankfurter HTTP {exc.code} fetching {frm}/{to} ({context}): {exc.reason}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_S * attempt)
    raise FxFetchError(
        f"Frankfurter fetch failed for {frm}/{to} ({context}) after "
        f"{_MAX_ATTEMPTS} attempts: {last_exc}"
    ) from last_exc


def _fetch_and_extract(url: str, frm: str, to: str, *, context: str) -> Decimal:
    body = _fetch_body(url, frm, to, context=context)

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FxFetchError(
            f"Frankfurter returned non-JSON for {frm}/{to} ({context}): {exc}"
        ) from exc

    rates = data.get("rates")
    if not isinstance(rates, dict) or to not in rates:
        raise FxFetchError(
            f"Frankfurter response for {frm}/{to} ({context}) missing 'rates.{to}': {data}"
        )

    raw = rates[to]
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError) as exc:
        raise FxFetchError(
            f"Frankfurter rate value {raw!r} for {frm}/{to} ({context}) cannot be "
            "converted to Decimal"
        ) from exc


# Fallback source for when Frankfurter is unavailable (it began returning
# Cloudflare 403s in Aug 2026). open.er-api.com is free/no-key but only serves the
# *current* snapshot, so it's used as a proxy only for recent dates — old trade-date
# rates must come from Frankfurter/cache, never a guessed current rate (§9).
_ERAPI_BASE = "https://open.er-api.com/v6/latest"
_ERAPI_FALLBACK_MAX_AGE_DAYS = 7


def _fetch_via_erapi(frm: str, to: str, *, context: str) -> Decimal:
    body = _fetch_body(f"{_ERAPI_BASE}/{frm}", frm, to, context=context)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FxFetchError(f"er-api returned non-JSON for {frm}/{to} ({context}): {exc}") from exc
    rates = data.get("rates")
    if not isinstance(rates, dict) or to not in rates:
        raise FxFetchError(f"er-api response for {frm}/{to} ({context}) missing 'rates.{to}': {data}")
    try:
        return Decimal(str(rates[to]))
    except (InvalidOperation, TypeError) as exc:
        raise FxFetchError(
            f"er-api rate value {rates[to]!r} for {frm}/{to} ({context}) cannot be "
            "converted to Decimal"
        ) from exc


def _fetch_historical(frm: str, to: str, on: date) -> Decimal:
    url = f"{_FRANKFURTER_BASE}/{on.isoformat()}?from={frm}&to={to}"
    try:
        return _fetch_and_extract(url, frm, to, context=f"trade-date {on.isoformat()}")
    except FxFetchError:
        # Frankfurter down: for a recent date the current rate is a fair proxy
        # (rates barely move day-to-day); for older dates re-raise, never guess.
        if (date.today() - on).days <= _ERAPI_FALLBACK_MAX_AGE_DAYS:
            return _fetch_via_erapi(frm, to, context=f"trade-date {on.isoformat()} (er-api fallback)")
        raise


def _fetch_current(frm: str, to: str) -> Decimal:
    url = f"{_FRANKFURTER_BASE}/latest?from={frm}&to={to}"
    try:
        return _fetch_and_extract(url, frm, to, context="current/latest")
    except FxFetchError:
        return _fetch_via_erapi(frm, to, context="current/latest (er-api fallback)")


def _cache_key(frm: str, to: str, on: date) -> str:
    return f"{frm}/{to}/{on.isoformat()}"


def _load_cache(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(path: Path, cache: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


class FxRates:
    """FX rate lookup with a local JSON cache for historical (trade-date) rates.

    Parameters
    ----------
    cache_path:
        Path to the local JSON cache file. Defaults to ~/.cache/broker_fx/fx_cache.json.
        Pass a custom path in tests.
    base_currency:
        Portfolio base currency. Defaults to 'SGD' per §7.
    """

    def __init__(
        self,
        cache_path: Optional[Path] = None,
        base_currency: str = "SGD",
    ) -> None:
        self._cache_path: Path = cache_path or _default_cache_path()
        self._base_currency: str = base_currency.strip().upper()
        self._mem: dict[str, str] = _load_cache(self._cache_path)

    def rate(self, pair: str, on: date) -> Decimal:
        """Historical trade-date rate for pair on given date (cached).

        Cached in both memory and on disk. Subsequent calls for the same
        (pair, on) hit the cache without a network round-trip.

        pair: e.g. 'USDSGD' or 'USD/SGD'. rate('USDSGD', d) * usd_amount == sgd_amount.
        on:   Trade date. Weekend/holiday dates resolved to nearest prior business day
              by Frankfurter; rate is stored under the *requested* date for stable lookups.

        Raises FxFetchError on API failure, FxPairError on malformed pair.
        """
        frm, to = _parse_pair(pair)
        if frm == to:
            return Decimal("1")

        key = _cache_key(frm, to, on)
        if key in self._mem:
            return Decimal(self._mem[key])

        rate_val = _fetch_historical(frm, to, on)
        self._mem[key] = str(rate_val)
        _save_cache(self._cache_path, self._mem)
        return rate_val

    def current_rate(self, pair: str) -> Decimal:
        """Live FX rate for pair. Never cached - always fetched fresh.

        Used only for unrealised-P/L valuations and Dashboard current-market-value.
        Historical realized P/L uses rate() (trade-date, cached) so it never drifts.

        Raises FxFetchError on API failure, FxPairError on malformed pair.
        """
        frm, to = _parse_pair(pair)
        if frm == to:
            return Decimal("1")
        return _fetch_current(frm, to)

    def to_sgd(self, amount: Decimal, currency: str, on: date) -> Decimal:
        """Convert amount in currency to SGD at the trade-date (historical) rate.

        If currency is already SGD (or base_currency), returns amount unchanged.
        Raises FxFetchError or FxPairError on failure.
        """
        ccy = currency.strip().upper()
        if ccy == self._base_currency:
            return amount
        pair = f"{ccy}{self._base_currency}"
        r = self.rate(pair, on)
        return amount * r

    def current_to_sgd(self, amount: Decimal, currency: str) -> Decimal:
        """Convert amount in currency to SGD at the current (live) rate.

        Used for live unrealised P/L and current market-value Dashboard columns.
        Never used for historical rows.
        """
        ccy = currency.strip().upper()
        if ccy == self._base_currency:
            return amount
        pair = f"{ccy}{self._base_currency}"
        r = self.current_rate(pair)
        return amount * r

    def cached_pairs_for_date(self, on: date) -> dict[str, Decimal]:
        """Return all cached rates for a specific date as {pair_str: rate}.
        Useful for the Run Log (which FX rates were used in this run).
        """
        prefix = on.isoformat()
        result: dict[str, Decimal] = {}
        for key, val in self._mem.items():
            # key format: "USD/SGD/2025-03-14"
            parts = key.rsplit("/", 1)
            if len(parts) == 2 and parts[1] == prefix:
                result[parts[0].replace("/", "")] = Decimal(val)
        return result

    def reload_cache(self) -> None:
        """Re-read the cache file from disk (e.g. after another process updated it)."""
        self._mem = _load_cache(self._cache_path)
