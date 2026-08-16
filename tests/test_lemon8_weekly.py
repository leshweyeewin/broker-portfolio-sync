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
from lemon8.blog import (
    commit_blog_drafts,
    prune_legacy_drafts,
    CommittedDraft,
    BlogCommitError,
)
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

def test_stale_cards_are_wiped_before_writing(tmp_path):
    # A leftover per-trade card from the old design (or a prior run) must not
    # survive into this run's cards/ folder.
    week_dir = tmp_path / "2026-08-14"
    stale = week_dir / "cards"
    stale.mkdir(parents=True)
    (stale / "AAPL-200-Call.png").write_bytes(b"stale")
    (stale / "AAPL-200-Call.svg").write_text("<svg/>", encoding="utf-8")

    run_weekly_journal(
        _sheet_with_closes(), today=_TODAY, output_root=str(tmp_path),
        notifier=lambda t: True, blog_settings={}, render_png=False,
    )

    assert not (stale / "AAPL-200-Call.png").exists()
    assert not (stale / "AAPL-200-Call.svg").exists()
    assert (stale / "table-01.svg").exists()  # this run's output is present


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
    # ...plus trade-log table page image(s) under cards/ (both trades fit one page)
    assert (week_dir / "cards" / "table-01.svg").exists()
    # the single blog covers both trades
    blog = (week_dir / "blog.md").read_text(encoding="utf-8")
    assert "AAPL" in blog and "TSLA 200 Put" in blog
    assert "2 trade(s)" in sent[0]

    # a self-contained local review page bundles the caption + transactions photo
    preview = (week_dir / "preview.html").read_text(encoding="utf-8")
    assert "<!doctype html>" in preview
    assert "<svg" in preview  # table pages embedded inline
    # the full blog body is NOT dumped into the preview (lives in blog.md)
    assert "Rationale & lessons" not in preview
    # self-contained: no scripts, no external stylesheet/image fetches
    assert "<script" not in preview and "src=" not in preview and "<link" not in preview


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

    def fake_pruner(*, settings):
        calls["pruned_settings"] = settings
        return ["content/2026-08-01-OLD.md"]

    result = run_weekly_journal(
        _sheet_with_closes(),
        today=_TODAY,
        output_root=str(tmp_path),
        notifier=lambda t: True,
        blog_settings={"token": "t", "repo": "me/blog", "branch": "lemon8-drafts", "path": "content"},
        blog_committer=fake_committer,
        draft_pruner=fake_pruner,
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
    # stale per-trade drafts are pruned as part of the same run
    assert calls["pruned_settings"]["repo"] == "me/blog"


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


# --------------------------------------------------------------------------- #
# legacy-draft prune (fake transport)
# --------------------------------------------------------------------------- #

def _listing_transport(entries, deletes):
    import json

    def transport(method, url, headers, body):
        if method == "GET":
            return 200, json.dumps(entries).encode()
        if method == "DELETE":
            deletes.append((url, json.loads(body)))
            return 200, b"{}"
        return 500, b""

    return transport


def test_prune_removes_only_legacy_per_trade_drafts():
    entries = [
        {"type": "file", "name": "2026-08-14-AAPL.md", "path": "content/2026-08-14-AAPL.md", "sha": "a1"},
        {"type": "file", "name": "2026-08-14-TSLA-200-Put.md", "path": "content/2026-08-14-TSLA-200-Put.md", "sha": "a2"},
        {"type": "file", "name": "2026-08-14-weekly-journal.md", "path": "content/2026-08-14-weekly-journal.md", "sha": "w1"},
        {"type": "file", "name": "README.md", "path": "content/README.md", "sha": "r1"},
        {"type": "dir", "name": "images", "path": "content/images", "sha": "d1"},
    ]
    deletes: list = []
    removed = prune_legacy_drafts(
        settings={"token": "t", "repo": "me/blog", "branch": "lemon8-drafts", "path": "content"},
        transport=_listing_transport(entries, deletes),
    )
    # only the two dated per-trade drafts go; weekly journal + README survive
    assert set(removed) == {
        "content/2026-08-14-AAPL.md",
        "content/2026-08-14-TSLA-200-Put.md",
    }
    assert all("weekly-journal" not in url and "README" not in url for url, _p in deletes)
    # delete payloads carry the file sha + target branch
    assert all(p["sha"] and p["branch"] == "lemon8-drafts" for _url, p in deletes)


def test_prune_noop_when_dir_missing():
    def transport(method, url, headers, body):
        return 404, b""

    assert prune_legacy_drafts(settings={"token": "t", "repo": "me/blog"}, transport=transport) == []


def test_prune_requires_config():
    with pytest.raises(BlogCommitError):
        prune_legacy_drafts(settings={"token": "", "repo": ""})


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
    from datetime import date
    from lemon8.card import render_trade_table_pages
    pos = ClosedPosition("Tiger", "NVDA", "stock", "2026-08-12", "USD", Decimal("300"), Decimal("405"), Decimal("12.5"))
    svg = render_trade_table_pages([pos], date(2026, 8, 14))[0]
    png = svg_to_png(svg)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 500
