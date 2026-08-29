"""Compatibility facade; import from :mod:`analytics.options.trade_plans` instead."""

from analytics.options.trade_plans import *  # noqa: F401,F403


if __name__ == "__main__":
    from analytics.options.trade_plans import main
    raise SystemExit(main())
