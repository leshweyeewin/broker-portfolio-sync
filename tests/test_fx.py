"""Tests for core/fx.py — FX rate module (step 4).

Tests are fully offline: we mock the HTTP layer via unittest.mock so no real
network calls are made. This keeps the suite deterministic, instant, and
usable in CI without credentials.

Coverage:
- SGD->SGD identity (no fetch, no network call)
- Cache hit returns same value without re-fetching (§7 reproducibility)
- Trade-date (rate) vs current (current_rate) are distinct calls and paths
- Missing rate raises FxFetchError (fail loud)
- Malformed pair raises FxPairError
- On API HTTP error raises FxFetchError
- On network error raises FxFetchError
- Cache is persisted to disk after a fetch and reloaded on next instantiation
- to_sgd() correctly multiplies amount * rate
- current_to_sgd() uses current_rate path (never the cache)
- cached_pairs_for_date() introspection
- reload_cache() picks up external changes to the cache file
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.fx import FxFetchError, FxPairError, FxRates, _parse_pair


# --------------------------------------------------------------------------- #
# Helper: fake HTTP response
# --------------------------------------------------------------------------- #
def _make_http_response(body: str, status: int = 200):
    mock_resp = MagicMock()
    mock_resp.read.return_value = body.encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _frankfurter_json(frm: str, to: str, rate: float, d: str = "2025-03-14") -> str:
    return json.dumps({"amount": 1.0, "base": frm, "date": d, "rates": {to: rate}})


# --------------------------------------------------------------------------- #
# _parse_pair unit tests
# --------------------------------------------------------------------------- #
class TestParsePair(unittest.TestCase):
    def test_six_char(self):
        self.assertEqual(_parse_pair("USDSGD"), ("USD", "SGD"))

    def test_slash(self):
        self.assertEqual(_parse_pair("USD/SGD"), ("USD", "SGD"))

    def test_lowercase(self):
        self.assertEqual(_parse_pair("usdsgd"), ("USD", "SGD"))

    def test_hyphen(self):
        self.assertEqual(_parse_pair("HKD-SGD"), ("HKD", "SGD"))

    def test_invalid_raises(self):
        with self.assertRaises(FxPairError):
            _parse_pair("US")

    def test_too_many_parts(self):
        with self.assertRaises(FxPairError):
            _parse_pair("USD/SGD/HKD")


# --------------------------------------------------------------------------- #
# FxRates — offline tests using a temp cache file
# --------------------------------------------------------------------------- #
class TestFxRates(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._cache_path = Path(self._tmpdir.name) / "fx_cache.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_fx(self) -> FxRates:
        return FxRates(cache_path=self._cache_path)

    # SGD -> SGD identity: no network call whatsoever
    def test_sgd_to_sgd_identity_rate(self):
        fx = self._make_fx()
        with patch("urllib.request.urlopen") as mock_open:
            r = fx.rate("SGDSGD", date(2025, 3, 14))
        self.assertEqual(r, Decimal("1"))
        mock_open.assert_not_called()

    def test_sgd_identity_to_sgd(self):
        fx = self._make_fx()
        with patch("urllib.request.urlopen") as mock_open:
            result = fx.to_sgd(Decimal("500"), "SGD", date(2025, 3, 14))
        self.assertEqual(result, Decimal("500"))
        mock_open.assert_not_called()

    # Cache hit returns same Decimal without re-fetching
    def test_cache_hit_no_refetch(self):
        fx = self._make_fx()
        body = _frankfurter_json("USD", "SGD", 1.3456)
        resp = _make_http_response(body)
        on = date(2025, 3, 14)

        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            r1 = fx.rate("USDSGD", on)
        self.assertEqual(mock_open.call_count, 1)

        # Second call: cache hit — no new HTTP request
        with patch("urllib.request.urlopen") as mock_open2:
            r2 = fx.rate("USDSGD", on)
        mock_open2.assert_not_called()
        self.assertEqual(r1, r2)

    # Cache is persisted to disk and reloaded by a new FxRates instance
    def test_cache_persisted_across_instances(self):
        on = date(2025, 3, 14)
        body = _frankfurter_json("USD", "SGD", 1.3456)
        resp = _make_http_response(body)

        fx1 = self._make_fx()
        with patch("urllib.request.urlopen", return_value=resp):
            r1 = fx1.rate("USDSGD", on)

        # New instance reads from disk
        fx2 = FxRates(cache_path=self._cache_path)
        with patch("urllib.request.urlopen") as mock_open2:
            r2 = fx2.rate("USDSGD", on)
        mock_open2.assert_not_called()
        self.assertEqual(r1, r2)

    # Trade-date rate and current_rate are distinct code paths
    def test_trade_date_vs_current_distinct_paths(self):
        on = date(2025, 3, 14)
        historical_body = _frankfurter_json("USD", "SGD", 1.3456, "2025-03-14")
        current_body = _frankfurter_json("USD", "SGD", 1.3890)

        resp_hist = _make_http_response(historical_body)
        resp_curr = _make_http_response(current_body)

        fx = self._make_fx()

        # Fetch historical
        with patch("urllib.request.urlopen", return_value=resp_hist):
            r_hist = fx.rate("USDSGD", on)

        # Fetch current
        with patch("urllib.request.urlopen", return_value=resp_curr):
            r_curr = fx.current_rate("USDSGD")

        # They hit different URLs and return different Decimals
        self.assertEqual(r_hist, Decimal("1.3456"))
        self.assertEqual(r_curr, Decimal("1.3890"))
        self.assertNotEqual(r_hist, r_curr)

    # current_rate is never stored in the cache
    def test_current_rate_not_cached(self):
        body = _frankfurter_json("USD", "SGD", 1.3890)
        resp = _make_http_response(body)
        fx = self._make_fx()

        with patch("urllib.request.urlopen", return_value=resp):
            fx.current_rate("USDSGD")

        # Cache file should be empty (or not exist)
        if self._cache_path.exists():
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            self.assertEqual(data, {})

    # FxFetchError on HTTP error status
    def test_http_error_raises_fx_fetch_error(self):
        import urllib.error
        fx = self._make_fx()

        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
            url="...", code=429, msg="Too Many Requests", hdrs=None, fp=None
        )):
            with self.assertRaises(FxFetchError) as cm:
                fx.rate("USDSGD", date(2025, 3, 14))
        self.assertIn("429", str(cm.exception))

    # FxFetchError on network error
    def test_network_error_raises_fx_fetch_error(self):
        import urllib.error
        fx = self._make_fx()

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            with self.assertRaises(FxFetchError):
                fx.rate("USDSGD", date(2025, 3, 14))

    # FxFetchError when rate key is missing in API response
    def test_missing_rate_key_raises_fx_fetch_error(self):
        body = json.dumps({"amount": 1.0, "base": "USD", "date": "2025-03-14", "rates": {}})
        resp = _make_http_response(body)
        fx = self._make_fx()

        with patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(FxFetchError) as cm:
                fx.rate("USDSGD", date(2025, 3, 14))
        self.assertIn("missing", str(cm.exception))

    # FxFetchError on non-JSON response
    def test_non_json_response_raises_fx_fetch_error(self):
        resp = _make_http_response("not json")
        fx = self._make_fx()

        with patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(FxFetchError):
                fx.rate("USDSGD", date(2025, 3, 14))

    # Malformed pair raises FxPairError
    def test_malformed_pair_raises_fx_pair_error(self):
        fx = self._make_fx()
        with self.assertRaises(FxPairError):
            fx.rate("TOOLONG", date(2025, 3, 14))

    # to_sgd multiplies amount by rate correctly
    def test_to_sgd_multiplication(self):
        on = date(2025, 3, 14)
        body = _frankfurter_json("USD", "SGD", 1.35)
        resp = _make_http_response(body)
        fx = self._make_fx()

        with patch("urllib.request.urlopen", return_value=resp):
            result = fx.to_sgd(Decimal("1000"), "USD", on)

        self.assertEqual(result, Decimal("1000") * Decimal("1.35"))

    # current_to_sgd uses current_rate (live) path
    def test_current_to_sgd(self):
        body = _frankfurter_json("HKD", "SGD", 0.175)
        resp = _make_http_response(body)
        fx = self._make_fx()

        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            result = fx.current_to_sgd(Decimal("10000"), "HKD")

        # Verify the URL used was the /latest endpoint
        call_args = mock_open.call_args[0][0]
        self.assertIn("latest", call_args.get_full_url())
        self.assertEqual(result, Decimal("10000") * Decimal("0.175"))

    # cached_pairs_for_date introspection
    def test_cached_pairs_for_date(self):
        on = date(2025, 3, 14)
        body = _frankfurter_json("USD", "SGD", 1.3456)
        resp = _make_http_response(body)
        fx = self._make_fx()

        with patch("urllib.request.urlopen", return_value=resp):
            fx.rate("USDSGD", on)

        cached = fx.cached_pairs_for_date(on)
        self.assertIn("USDSGD", cached)
        self.assertEqual(cached["USDSGD"], Decimal("1.3456"))

    # reload_cache picks up external changes
    def test_reload_cache(self):
        fx = self._make_fx()
        on = date(2025, 1, 15)

        # Manually write a cache entry from "outside"
        data = {"USD/SGD/2025-01-15": "1.3500"}
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(data), encoding="utf-8")

        fx.reload_cache()

        with patch("urllib.request.urlopen") as mock_open:
            r = fx.rate("USDSGD", on)
        mock_open.assert_not_called()
        self.assertEqual(r, Decimal("1.3500"))

    # Different dates are stored under separate cache keys
    def test_different_dates_separate_cache_keys(self):
        on1 = date(2025, 3, 14)
        on2 = date(2025, 3, 17)
        body1 = _frankfurter_json("USD", "SGD", 1.3456, "2025-03-14")
        body2 = _frankfurter_json("USD", "SGD", 1.3500, "2025-03-17")
        fx = self._make_fx()

        with patch("urllib.request.urlopen", return_value=_make_http_response(body1)):
            r1 = fx.rate("USDSGD", on1)
        with patch("urllib.request.urlopen", return_value=_make_http_response(body2)):
            r2 = fx.rate("USDSGD", on2)

        self.assertEqual(r1, Decimal("1.3456"))
        self.assertEqual(r2, Decimal("1.3500"))
        self.assertNotEqual(r1, r2)

    # HKD/SGD pair works correctly
    def test_hkd_sgd_pair(self):
        on = date(2025, 3, 14)
        body = _frankfurter_json("HKD", "SGD", 0.1741)
        resp = _make_http_response(body)
        fx = self._make_fx()

        with patch("urllib.request.urlopen", return_value=resp):
            result = fx.to_sgd(Decimal("5000"), "HKD", on)

        expected = Decimal("5000") * Decimal("0.1741")
        self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
