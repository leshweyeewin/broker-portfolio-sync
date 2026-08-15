"""Tests for the Lemon8 weekly job, blog committer, and PNG render.

Offline: FakeSheetClient for the sheet, tmp_path for output, injected notifier
and blog transport/committer so nothing hits Telegram or GitHub. The PNG test
does a real rasterize (deps are in requirements.txt).
"""

from __future__ import annotations

from datetime import date

import pytest

from lemon8.weekly_job import (
    select_recent_closes,
    run_weekly_journal,
    _weekly_blog_file,
    _slug,
)
from lemon8.blog import commit_blog_drafts, CommittedDraft, BlogCommitError
from lemon8.render import svg_to_png
from lemon8.reader import ClosedPosition
from lemon8.journal import generate_weekly_journal
from sheets.writer import STOCKS_HEADERS, OPTIONS_HEADERS, TAB_STOCKS, TAB_OPTIONS
from tests.test_writer import FakeSheetClient

_SUMMARY_BLOCK = [["Total P/L", ""], ["Total Fees", ""]]
_TODAY = date(2026, 8, 14)  # window [2026-08-07, 2026-08-14]


def _sheet_with_closes() -> FakeSheetClient:
    client = FakeSheetClient([TAB_STOCKS, TAB_OPTIONS])
    stocks = _SUMMARY_BLOCK + [
        [str(h) for h in STOCKS_HEADERS],
        # AAPL closed 2026-08-12 — inside window
        ["2026-08-12", "Tiger", "AAPL", "SELL", 10, 180.0, 1800.0, 1.5, "USD", "Closed", 300.0, 405.0, "s1"],
        # NVDA closed 2026-07-30 — outside window
        ["2026-07-30", "Tiger", "NVDA", "SELL", 5, 120.0, 600.0, 1.0, "USD", "Closed", 50.0, 67.0, "s2"],
        # Open row — never journaled
        ["2026-08-13", "Tiger", "MSFT", "BUY", 3, 500.0, -1500.0, 1.0, "USD", "Open", "", "", "s3"],
    ]
    client.batch_update_values([{"range": f"{TAB_STOCKS}!A1", "values": stocks}])

    options = _SUMMARY_BLOCK + [
        [str(h) for h in OPTIONS_HEADERS],
        # TSLA put closed 2026-08-10 — inside window
        ["2026-08-10", "Tiger", "Short Put", "TSLA", "Put", "200", 1, "2026-04-18", "BUY", 1.0, -100.0, 1.0, "USD", "Closed", 400.0, 540.0, "o1"],
    ]
    client.batch_update_values([{"range": f"{TAB_OPTIONS}!A1", "values": options}])
    return client


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #

def test_select_recent_closes_keeps_only_last_week():
    positions = [
        ClosedPosition("Tiger", "AAPL", "stock", "2026-08-12", "USD", None, None, None),
        ClosedPosition("Tiger", "NVDA", "stock", "2026-07-30", "USD", None, None, None),
        ClosedPosition("Tiger", "EDGE", "stock", "2026-08-07", "USD", None, None, None),  # == cutoff
    ]
    kept = select_recent_closes(positions, today=_TODAY, window_days=7)
    assert [p.symbol for p in kept] == ["EDGE", "AAPL"]  # sorted by close date


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #

def test_run_writes_files_and_notifies(tmp_path):
    sent: list[str] = []
    result = run_weekly_journal(
        _sheet_with_closes(),
        today=_TODAY,
        output_root=str(tmp_path),
        notifier=lambda t: sent.append(t) or True,
        blog_settings={},          # GitHub not configured -> skipped
        render_png=False,          # fast; PNG covered separately
    )

    assert result.count == 2 and result.committed == 0 and result.delivered is True
    week_dir = tmp_path / "2026-08-14"
    # ONE consolidated post per week — a single blog + single caption...
    assert (week_dir / "index.md").exists()
    assert (week_dir / "blog.md").exists()
    assert (week_dir / "caption.txt").exists()
    # ...plus one screenshot card per trade under cards/.
    for slug in ("AAPL", "TSLA-200-Put"):
        assert (week_dir / "cards" / f"{slug}.svg").exists()
    # the single blog covers both trades
    blog = (week_dir / "blog.md").read_text(encoding="utf-8")
    assert "AAPL" in blog and "TSLA 200 Put" in blog
    assert "2 trade(s)" in sent[0]


def test_run_with_no_recent_closes_still_notifies(tmp_path):
    client = FakeSheetClient([TAB_STOCKS, TAB_OPTIONS])
    client.batch_update_values([{"range": f"{TAB_STOCKS}!A1",
                                 "values": _SUMMARY_BLOCK + [[str(h) for h in STOCKS_HEADERS]]}])
    client.batch_update_values([{"range": f"{TAB_OPTIONS}!A1",
                                 "values": _SUMMARY_BLOCK + [[str(h) for h in OPTIONS_HEADERS]]}])
    sent: list[str] = []
    result = run_weekly_journal(
        client, today=_TODAY, output_root=str(tmp_path),
        notifier=lambda t: sent.append(t) or True, blog_settings={}, render_png=False,
    )
    assert result.count == 0
    assert "no trades closed" in sent[0].lower()


def test_run_commits_blog_when_configured(tmp_path):
    calls = {}

    def fake_committer(items, *, settings):
        calls["items"] = items
        calls["settings"] = settings
        return [CommittedDraft(path=f"drafts/{fn}", url="http://x", updated=False) for fn, _ in items]

    result = run_weekly_journal(
        _sheet_with_closes(),
        today=_TODAY,
        output_root=str(tmp_path),
        notifier=lambda t: True,
        blog_settings={"token": "t", "repo": "me/blog", "branch": "lemon8-drafts", "path": "content"},
        blog_committer=fake_committer,
        render_png=False,
    )
    # ONE weekly blog draft is committed, not one per trade
    assert result.committed == 1
    assert len(calls["items"]) == 1
    (filename, md) = calls["items"][0]
    assert filename == "2026-08-14-weekly-journal.md"
    assert "draft: true" in md
    # the single draft covers both of the week's trades
    assert "AAPL" in md and "TSLA 200 Put" in md
    assert calls["settings"]["repo"] == "me/blog"


def test_weekly_blog_file_has_frontmatter_and_covers_all_trades():
    positions = [
        ClosedPosition("Tiger", "AAPL", "stock", "2026-08-12", "USD", None, None, None),
        ClosedPosition("Tiger", "TSLA", "option", "2026-08-10", "USD", None, None, None,
                       option_type="Put", strike="200"),
    ]
    journal = generate_weekly_journal(positions, _TODAY)
    filename, content = _weekly_blog_file(journal)
    assert filename == "2026-08-14-weekly-journal.md"
    assert content.startswith("---\n") and "draft: true" in content
    assert "AAPL" in content and "TSLA 200 Put" in content


def test_slug_sanitizes_labels():
    assert _slug("TSLA 200 Put") == "TSLA-200-Put"
    assert _slug("BRK.B") == "BRK-B"


# --------------------------------------------------------------------------- #
# blog committer (fake transport)
# --------------------------------------------------------------------------- #

class _FakeGitHub:
    """Programmable transport: GET returns get_status/get_body, PUT records + 201."""

    def __init__(self, get_status=404, get_body=b""):
        self._get = (get_status, get_body)
        self.puts: list[tuple[str, dict]] = []

    def __call__(self, method, url, headers, body):
        if method == "GET":
            return self._get
        import json
        self.puts.append((url, json.loads(body)))
        return 201, json.dumps({"content": {"html_url": f"{url}#committed"}}).encode()


def test_commit_creates_new_file():
    gh = _FakeGitHub(get_status=404)
    out = commit_blog_drafts(
        [("2026-08-14-AAPL.md", "# post\n")],
        settings={"token": "t", "repo": "me/blog", "branch": "lemon8-drafts", "path": "content/drafts"},
        transport=gh,
    )
    assert len(out) == 1 and out[0].updated is False
    url, payload = gh.puts[0]
    assert url.endswith("/repos/me/blog/contents/content/drafts/2026-08-14-AAPL.md")
    assert payload["branch"] == "lemon8-drafts" and "sha" not in payload


def test_commit_updates_existing_file_with_sha():
    import json
    gh = _FakeGitHub(get_status=200, get_body=json.dumps({"sha": "deadbeef"}).encode())
    out = commit_blog_drafts(
        [("2026-08-14-AAPL.md", "# post\n")],
        settings={"token": "t", "repo": "me/blog"},
        transport=gh,
    )
    assert out[0].updated is True
    _url, payload = gh.puts[0]
    assert payload["sha"] == "deadbeef"


def test_commit_requires_config():
    with pytest.raises(BlogCommitError):
        commit_blog_drafts([("x.md", "y")], settings={"token": "", "repo": ""})


def test_commit_raises_on_put_error():
    def transport(method, url, headers, body):
        return (404, b"") if method == "GET" else (422, b'{"message":"bad"}')
    with pytest.raises(BlogCommitError):
        commit_blog_drafts([("x.md", "y")], settings={"token": "t", "repo": "me/blog"}, transport=transport)


# --------------------------------------------------------------------------- #
# PNG render (real)
# --------------------------------------------------------------------------- #

def test_svg_to_png_produces_png_bytes():
    from decimal import Decimal
    from lemon8.card import render_card_svg
    pos = ClosedPosition("Tiger", "NVDA", "stock", "2026-08-12", "USD", Decimal("300"), Decimal("405"), Decimal("12.5"))
    png = svg_to_png(render_card_svg(pos))
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 500
