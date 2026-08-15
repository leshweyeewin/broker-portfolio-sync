"""Tests for pancherry_export.publish — the auto-PR of the data files.

Fully offline: a stateful FakeGitHub transport stands in for the GitHub API and
records every call, so we assert branch creation, per-file commits, PR
open/update, and the no-op path without any network.
"""

from __future__ import annotations

import base64
import json

import pytest

from pancherry_export.publish import PancherryPublishError, publish_draft_pr

_SETTINGS = {"token": "tok", "repo": "owner/repo", "branch": "pancherry-drafts", "base": "main"}


class FakeGitHub:
    """Minimal in-memory GitHub contents/refs/pulls API."""

    def __init__(self, *, branch_exists=False, files=None, open_pr=False, no_diff=False):
        self.calls: list[tuple[str, str]] = []
        self.branch_exists = branch_exists
        self.files: dict[str, bytes] = dict(files or {})   # path on the drafts branch
        self.open_pr = open_pr
        self.no_diff = no_diff                              # PR create → 422 "No commits between"
        self.created_pr = False
        self.patched = False

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))

        if method == "GET" and "/git/ref/heads/main" in url:
            return 200, json.dumps({"object": {"sha": "basesha"}}).encode()
        if method == "GET" and "/git/ref/heads/pancherry-drafts" in url:
            return (200, b'{"object":{"sha":"x"}}') if self.branch_exists else (404, b"{}")
        if method == "POST" and url.endswith("/git/refs"):
            self.branch_exists = True
            return 201, b"{}"

        if method == "GET" and "/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            if path in self.files:
                return 200, json.dumps(
                    {"sha": "filesha", "content": base64.b64encode(self.files[path]).decode()}
                ).encode()
            return 404, b"{}"
        if method == "PUT" and "/contents/" in url:
            path = url.split("/contents/")[1]
            self.files[path] = base64.b64decode(json.loads(body)["content"])
            return 201, b"{}"

        if method == "GET" and "/pulls?" in url:
            if self.open_pr:
                return 200, json.dumps(
                    [{"number": 7, "html_url": "https://github.com/owner/repo/pull/7"}]
                ).encode()
            return 200, b"[]"
        if method == "POST" and url.endswith("/pulls"):
            if self.no_diff:
                return 422, b'{"message":"No commits between main and pancherry-drafts"}'
            self.created_pr = True
            return 201, json.dumps({"html_url": "https://github.com/owner/repo/pull/8"}).encode()
        if method == "PATCH" and "/pulls/" in url:
            self.patched = True
            return 200, b"{}"

        raise AssertionError(f"unexpected call: {method} {url}")


def _files(tmp_path, **contents):
    out = []
    for name, text in contents.items():
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        out.append((f"src/data/{name}", str(p)))
    return out


def test_creates_branch_commits_both_files_and_opens_draft_pr(tmp_path):
    files = _files(tmp_path, **{"openPositions.ts": "open-data", "weeklyJournals.ts": "journal-data"})
    gh = FakeGitHub(branch_exists=False)

    res = publish_draft_pr(files, settings=_SETTINGS, title="T", body="B", transport=gh)

    assert res.created is True
    assert res.committed == 2
    assert res.url.endswith("/pull/8")
    assert gh.branch_exists is True
    assert any(m == "POST" and u.endswith("/git/refs") for m, u in gh.calls)   # branch created
    assert sum(1 for m, u in gh.calls if m == "PUT") == 2                       # both files committed


def test_updates_existing_pr_and_skips_unchanged_files(tmp_path):
    files = _files(tmp_path, **{"openPositions.ts": "open-data", "weeklyJournals.ts": "journal-data"})
    gh = FakeGitHub(
        branch_exists=True,
        open_pr=True,
        files={"src/data/openPositions.ts": b"open-data",
               "src/data/weeklyJournals.ts": b"journal-data"},
    )

    res = publish_draft_pr(files, settings=_SETTINGS, title="T", body="B", transport=gh)

    assert res.created is False        # reused the open PR
    assert res.committed == 0          # identical bytes → no commit
    assert res.url.endswith("/pull/7")
    assert gh.patched is True          # body refreshed
    assert gh.created_pr is False
    assert not any(m == "PUT" for m, _ in gh.calls)


def test_commits_only_the_changed_file(tmp_path):
    files = _files(tmp_path, **{"openPositions.ts": "NEW-open", "weeklyJournals.ts": "journal-data"})
    gh = FakeGitHub(
        branch_exists=True,
        open_pr=True,
        files={"src/data/openPositions.ts": b"OLD-open",
               "src/data/weeklyJournals.ts": b"journal-data"},
    )

    res = publish_draft_pr(files, settings=_SETTINGS, title="T", body="B", transport=gh)

    assert res.committed == 1
    assert sum(1 for m, _ in gh.calls if m == "PUT") == 1


def test_opens_pr_when_branch_ahead_but_no_open_pr(tmp_path):
    # Files already committed on the branch (committed==0 this run) and no open
    # PR — e.g. a prior run's PR step failed. We must still open the PR.
    files = _files(tmp_path, **{"openPositions.ts": "open-data"})
    gh = FakeGitHub(branch_exists=True, open_pr=False,
                    files={"src/data/openPositions.ts": b"open-data"})

    res = publish_draft_pr(files, settings=_SETTINGS, title="T", body="B", transport=gh)

    assert res.committed == 0          # nothing new to commit
    assert res.created is True          # ...but the PR still gets opened
    assert res.url.endswith("/pull/8")


def test_no_pr_when_branch_has_no_diff(tmp_path):
    # Branch identical to base → GitHub 422 "No commits between" → nothing to do.
    files = _files(tmp_path, **{"openPositions.ts": "open-data"})
    gh = FakeGitHub(branch_exists=True, open_pr=False, no_diff=True,
                    files={"src/data/openPositions.ts": b"open-data"})

    res = publish_draft_pr(files, settings=_SETTINGS, title="T", body="B", transport=gh)

    assert res.url == ""
    assert res.created is False


def test_raises_when_not_configured():
    with pytest.raises(PancherryPublishError):
        publish_draft_pr([], settings={"token": "", "repo": ""}, title="T", body="B",
                         transport=lambda *a: (200, b"{}"))
