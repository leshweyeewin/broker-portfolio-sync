"""Tests for core.ticker_names — the broker-name cache feeding the site."""

from __future__ import annotations

from dataclasses import dataclass

from core.ticker_names import load_names, merge_names, refresh_from_brokers


@dataclass
class _Pos:
    symbol: str
    name: str = ""


def test_merge_writes_and_reloads(tmp_path):
    path = tmp_path / "ticker_names.json"
    merge_names([_Pos("MSFT", "Microsoft"), _Pos("NVDA", "NVIDIA")], path)
    assert load_names(path) == {"MSFT": "Microsoft", "NVDA": "NVIDIA"}


def test_merge_is_additive_and_keeps_prior_names(tmp_path):
    path = tmp_path / "ticker_names.json"
    merge_names([_Pos("MSFT", "Microsoft")], path)
    # A later run that doesn't include MSFT (or gives it no name) must not drop it.
    merge_names([_Pos("MSFT", ""), _Pos("GOOG", "Alphabet")], path)
    assert load_names(path) == {"MSFT": "Microsoft", "GOOG": "Alphabet"}


def test_nonempty_name_overwrites(tmp_path):
    path = tmp_path / "ticker_names.json"
    merge_names([_Pos("BE", "Bloom")], path)
    merge_names([_Pos("BE", "Bloom Energy")], path)
    assert load_names(path)["BE"] == "Bloom Energy"


def test_symbols_without_names_are_skipped(tmp_path):
    path = tmp_path / "ticker_names.json"
    merge_names([_Pos("07709", ""), _Pos("MSFT", "Microsoft")], path)
    assert load_names(path) == {"MSFT": "Microsoft"}


def test_load_missing_or_bad_file_returns_empty(tmp_path):
    assert load_names(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert load_names(bad) == {}


# --------------------------------------------------------------------------- #
# refresh_from_brokers — the standalone, decoupled broker lookup
# --------------------------------------------------------------------------- #

class _Adapter:
    def __init__(self, name, positions, *, boom=False):
        self.name = name
        self._positions = positions
        self._boom = boom

    def fetch_positions(self):
        if self._boom:
            raise RuntimeError("OpenD down")
        return self._positions


def test_refresh_pulls_names_from_all_adapters(tmp_path):
    path = tmp_path / "ticker_names.json"
    adapters = [
        _Adapter("Tiger", [_Pos("GOOG", "Alphabet"), _Pos("NVDA", "NVIDIA")]),
        _Adapter("Longbridge", [_Pos("700", "Tencent")]),
    ]
    merged = refresh_from_brokers(path, adapters=adapters)
    assert merged == {"GOOG": "Alphabet", "NVDA": "NVIDIA", "700": "Tencent"}
    assert load_names(path) == merged


def test_refresh_is_failsoft_when_one_broker_is_down(tmp_path):
    path = tmp_path / "ticker_names.json"
    merge_names([_Pos("SNDK", "SanDisk")], path)   # a MooMoo name cached earlier
    adapters = [
        _Adapter("Tiger", [_Pos("GOOG", "Alphabet")]),
        _Adapter("MooMoo", [], boom=True),          # OpenD down this run
    ]
    merged = refresh_from_brokers(path, adapters=adapters)
    # Tiger's name lands; the down broker doesn't sink it; the prior MooMoo name is kept.
    assert merged["GOOG"] == "Alphabet"
    assert merged["SNDK"] == "SanDisk"
