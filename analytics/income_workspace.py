"""Compatibility facade; run :mod:`analytics.options.income_workspace` instead."""

from analytics.options.income_workspace import *  # noqa: F401,F403


if __name__ == "__main__":
    from analytics.options.income_workspace import main
    raise SystemExit(main())
