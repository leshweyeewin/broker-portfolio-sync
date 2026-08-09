"""Lemon8 journal module (step 10 — BUILD_SPEC.md §11b).

A **separate, read-only** companion to the sync pipeline. It only *reads* the
Google Sheet the pipeline writes and turns closed positions into social-journal
copy. It never imports or touches the sync/write path.

Privacy is load-bearing: everything renders **percentages + reasoning** and, by
default, hides absolute dollar amounts and portfolio size. Showing dollars is an
explicit per-post opt-in (``show_dollar_amounts=True``) — never the default,
never inferred.
"""
