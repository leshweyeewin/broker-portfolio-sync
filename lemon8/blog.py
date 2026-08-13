"""Commit Lemon8 blog drafts to the blog repo via the GitHub API (§11b).

The user's blog (pancherry.com/trading) is a GitHub repo built by Cloudflare
Pages. This commits each weekly retrospective as a **draft** — to a dedicated
drafts branch (NOT the production branch Pages builds) *and* with
``draft: true`` frontmatter — so nothing is ever published on the live domain
unattended (§11b: "the job never puts content live … unattended").

Plain ``urllib`` (no SDK). The HTTP call goes through an injectable transport so
tests assert on method/URL/payload without touching GitHub.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

_API = "https://api.github.com"
_TIMEOUT = 15

# (method, url, headers, body_bytes | None) -> (status_code, response_bytes)
Transport = Callable[[str, str, dict, Optional[bytes]], "tuple[int, bytes]"]


class BlogCommitError(RuntimeError):
    """Raised when a draft can't be committed (config, network, or API error)."""


@dataclass(frozen=True)
class CommittedDraft:
    path: str
    url: str        # html_url of the committed file (may be "")
    updated: bool   # True if it replaced an existing file at that path


def _urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes]):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        # HTTP errors (404/409/422) are handled by the caller, not raised.
        return exc.code, exc.read() if exc.fp else b""
    except urllib.error.URLError as exc:
        raise BlogCommitError(f"GitHub network error: {exc.reason}") from exc


def commit_blog_drafts(
    items: list[tuple[str, str]],
    *,
    settings: dict,
    transport: Optional[Transport] = None,
) -> list[CommittedDraft]:
    """Commit each ``(filename, markdown)`` to the drafts branch.

    ``settings`` is ``config.settings.get_blog_settings()`` — needs ``token`` and
    ``repo`` ("owner/name"); ``branch`` and ``path`` have defaults. The branch must
    already exist. Raises ``BlogCommitError`` on any failure (the weekly job calls
    this best-effort and logs rather than crashing).
    """
    token = settings.get("token")
    repo = settings.get("repo")
    if not token or not repo:
        raise BlogCommitError("GitHub not configured (need GITHUB_TOKEN and BLOG_REPO).")
    branch = settings.get("branch") or "lemon8-drafts"
    prefix = (settings.get("path") or "").strip("/")
    transport = transport or _urllib_transport

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "broker-portfolio-sync/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    out: list[CommittedDraft] = []
    for filename, content in items:
        path = f"{prefix}/{filename}" if prefix else filename
        url = f"{_API}/repos/{repo}/contents/{path}"

        # A file already at this path (same week re-run) needs its sha to update.
        status, body = transport("GET", f"{url}?ref={branch}", headers, None)
        sha: Optional[str] = None
        if status == 200:
            sha = json.loads(body or b"{}").get("sha")
        elif status != 404:
            raise BlogCommitError(f"GitHub GET {path} -> {status}: {body[:200]!r}")

        payload = {
            "message": f"lemon8: draft journal {filename}",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        put_headers = {**headers, "Content-Type": "application/json"}
        pstatus, pbody = transport("PUT", url, put_headers, json.dumps(payload).encode("utf-8"))
        if pstatus not in (200, 201):
            raise BlogCommitError(f"GitHub PUT {path} -> {pstatus}: {pbody[:300]!r}")

        parsed = json.loads(pbody or b"{}")
        out.append(
            CommittedDraft(
                path=path,
                url=(parsed.get("content") or {}).get("html_url", ""),
                updated=sha is not None,
            )
        )
    return out
