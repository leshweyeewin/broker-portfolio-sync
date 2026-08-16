"""Persist a ticker → security-name map captured from broker positions.

The broker position feeds carry each instrument's display name (MooMoo
``stock_name``, Longbridge ``symbol_name``, Tiger contract ``name``). The daily
sync captures those here so the pancherry open-positions grid can show company
names automatically — no hand-maintained list for new holdings.

Best-effort and additive: a symbol whose broker didn't supply a name simply
isn't cached (a previously-cached name is kept), and any I/O error is swallowed
so name-caching never breaks the sync.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)


def load_names(path) -> dict[str, str]:
    """Load the ``{ticker: name}`` cache; ``{}`` if missing or unreadable."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("Could not read ticker-name cache at %s", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def merge_names(positions: Iterable, path) -> dict[str, str]:
    """Merge ``{symbol: name}`` from ``positions`` into the JSON cache at ``path``.

    Existing entries survive; a non-empty broker name overwrites. Symbols with no
    name are left as-is (never blanked). Returns the merged map. Never raises —
    an I/O failure is logged and the sync continues.
    """
    path = Path(path)
    merged = load_names(path)
    for p in positions:
        symbol = (getattr(p, "symbol", "") or "").strip()
        name = (getattr(p, "name", "") or "").strip()
        if symbol and name:
            merged[symbol] = name
    try:
        path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        log.warning("Could not write ticker-name cache at %s", path, exc_info=True)
    return merged


def refresh_from_brokers(path, *, adapters: Optional[Iterable] = None) -> dict[str, str]:
    """Connect to the brokers, read live positions, and update the name cache.

    Standalone — independent of the daily sync. Reuses the same adapter wiring
    (``run._build_adapters``): Tiger + Longbridge connect directly; MooMoo needs
    its OpenD gateway up. Fail-soft per broker — one that's unconfigured or
    unreachable is skipped and its previously-cached names are kept — so a partial
    connection still refreshes what it can. Returns the merged map.
    """
    if adapters is None:
        from run import _build_adapters
        adapters = _build_adapters()

    positions: list = []
    for adapter in adapters:
        label = getattr(adapter, "name", adapter.__class__.__name__)
        try:
            positions.extend(adapter.fetch_positions())
        except Exception:  # a broker being down must not sink the others
            log.warning("Name refresh: %s fetch_positions failed; skipping.", label, exc_info=True)

    merged = merge_names(positions, path)
    log.info("Ticker-name cache refreshed: %d name(s) at %s", len(merged), path)
    return merged


def main(argv=None) -> int:
    import argparse
    from config.settings import get_ticker_names_path

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Refresh the ticker-name cache from the brokers (for the pancherry open-positions grid)."
    )
    parser.add_argument("--path", default=None, help="Cache file path (default: TICKER_NAMES_PATH).")
    args = parser.parse_args(argv)

    path = args.path or get_ticker_names_path()
    merged = refresh_from_brokers(path)
    print(f"Refreshed {len(merged)} ticker name(s) → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
