"""Open a Draft PR against the pancherry repo with the generated data files.

Reads the **local** ``.ts`` files (so any prose you edited before running is
what lands in the PR), commits them to a drafts branch via the GitHub contents
API, and opens or updates a Draft PR into the base branch. No ``gh`` CLI and no
local git auth — just a ``GITHUB_TOKEN`` with ``repo`` scope.

Nothing merges automatically: the PR is the review gate, and the drafts branch is
never the branch Cloudflare Pages builds, so the public site can't change
unattended. Re-runs update the same branch/PR in place (unchanged files are
skipped, so an idempotent run makes no commit).

Plain ``urllib`` through an injectable transport, so tests assert on
method/URL/payload without touching GitHub.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

_API = "https://api.github.com"
_TIMEOUT = 15

log = logging.getLogger(__name__)

# (method, url, headers, body_bytes | None) -> (status_code, response_bytes)
Transport = Callable[[str, str, dict, Optional[bytes]], "tuple[int, bytes]"]


class PancherryPublishError(RuntimeError):
    """Raised when the PR can't be prepared (config, network, or API error)."""


@dataclass(frozen=True)
class PRResult:
    url: str          # html_url of the PR ("" if nothing to do)
    created: bool     # True = newly opened, False = updated an existing PR
    committed: int    # files that actually changed on the branch


def publish_draft_pr(
    files: list[tuple[str, str]],
    *,
    settings: dict,
    title: str,
    body: str,
    transport: Optional[Transport] = None,
) -> PRResult:
    """Commit each ``(repo_path, local_path)`` to the drafts branch and ensure a
    Draft PR is open.

    ``settings`` needs ``token`` + ``repo`` ("owner/name"); ``branch`` and
    ``base`` have defaults. Raises ``PancherryPublishError`` on any hard failure.
    """
    token = settings.get("token")
    repo = settings.get("repo")
    if not token or not repo:
        raise PancherryPublishError(
            "GitHub not configured (need GITHUB_TOKEN and PANCHERRY_REPO_SLUG)."
        )
    branch = settings.get("branch") or "pancherry-drafts"
    base = settings.get("base") or "main"
    transport = transport or _urllib_transport

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "broker-portfolio-sync/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    _ensure_branch(repo, base, branch, headers=headers, transport=transport)

    committed = 0
    for repo_path, local_path in files:
        content = Path(local_path).read_bytes()
        if _commit_file(repo, branch, repo_path, content, headers=headers, transport=transport):
            committed += 1

    return _open_or_update_pr(
        repo, branch, base, title, body, committed, headers=headers, transport=transport
    )


# --------------------------------------------------------------------------- #
# GitHub calls
# --------------------------------------------------------------------------- #

def _ensure_branch(repo: str, base: str, branch: str, *, headers: dict, transport: Transport) -> None:
    """Create ``branch`` off ``base``'s HEAD if it doesn't already exist."""
    status, _ = transport("GET", f"{_API}/repos/{repo}/git/ref/heads/{branch}", headers, None)
    if status == 200:
        return
    if status != 404:
        raise PancherryPublishError(f"GitHub GET ref {branch} -> {status}")

    bstatus, bbody = transport("GET", f"{_API}/repos/{repo}/git/ref/heads/{base}", headers, None)
    if bstatus != 200:
        raise PancherryPublishError(f"GitHub GET base ref {base} -> {bstatus}: {bbody[:200]!r}")
    sha = (json.loads(bbody or b"{}").get("object") or {}).get("sha")
    if not sha:
        raise PancherryPublishError(f"No commit sha for base branch {base!r}")

    payload = {"ref": f"refs/heads/{branch}", "sha": sha}
    cstatus, cbody = transport(
        "POST", f"{_API}/repos/{repo}/git/refs",
        {**headers, "Content-Type": "application/json"},
        json.dumps(payload).encode("utf-8"),
    )
    if cstatus not in (200, 201):
        raise PancherryPublishError(f"GitHub create branch {branch} -> {cstatus}: {cbody[:200]!r}")


def _commit_file(
    repo: str, branch: str, path: str, content: bytes, *, headers: dict, transport: Transport
) -> bool:
    """PUT ``content`` at ``path`` on ``branch``. Returns True if a commit was
    made, False if the branch already holds identical bytes (no-op, no churn)."""
    url = f"{_API}/repos/{repo}/contents/{path}"
    status, gbody = transport("GET", f"{url}?ref={branch}", headers, None)

    sha: Optional[str] = None
    if status == 200:
        data = json.loads(gbody or b"{}")
        sha = data.get("sha")
        existing = (data.get("content") or "").replace("\n", "")
        if existing:
            try:
                if base64.b64decode(existing) == content:
                    return False
            except (ValueError, TypeError):
                pass  # can't compare → fall through and PUT
    elif status != 404:
        raise PancherryPublishError(f"GitHub GET {path} -> {status}: {gbody[:200]!r}")

    payload = {
        "message": f"pancherry: update {path}",
        "content": base64.b64encode(content).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    pstatus, pbody = transport(
        "PUT", url, {**headers, "Content-Type": "application/json"},
        json.dumps(payload).encode("utf-8"),
    )
    if pstatus not in (200, 201):
        raise PancherryPublishError(f"GitHub PUT {path} -> {pstatus}: {pbody[:300]!r}")
    return True


def _open_or_update_pr(
    repo: str, branch: str, base: str, title: str, body: str, committed: int,
    *, headers: dict, transport: Transport,
) -> PRResult:
    """Return the existing open PR (patching its body) or open a new Draft PR."""
    owner = repo.split("/", 1)[0]
    lstatus, lbody = transport(
        "GET", f"{_API}/repos/{repo}/pulls?state=open&head={owner}:{branch}", headers, None
    )
    if lstatus == 200:
        prs = json.loads(lbody or b"[]")
        if prs:
            pr = prs[0]
            transport(
                "PATCH", f"{_API}/repos/{repo}/pulls/{pr['number']}",
                {**headers, "Content-Type": "application/json"},
                json.dumps({"body": body}).encode("utf-8"),
            )
            return PRResult(url=pr.get("html_url", ""), created=False, committed=committed)

    # No open PR. Try to open one regardless of this run's commit count — the
    # branch may already be ahead of base from an earlier run whose PR step
    # failed. GitHub tells us via 422 if there is genuinely no diff.
    payload = {"title": title, "head": branch, "base": base, "body": body, "draft": True}
    cstatus, cbody = transport(
        "POST", f"{_API}/repos/{repo}/pulls",
        {**headers, "Content-Type": "application/json"},
        json.dumps(payload).encode("utf-8"),
    )
    if cstatus in (200, 201):
        return PRResult(url=json.loads(cbody or b"{}").get("html_url", ""), created=True, committed=committed)
    if cstatus == 422 and b"No commits between" in cbody:
        return PRResult(url="", created=False, committed=committed)   # branch == base, nothing to do
    raise PancherryPublishError(f"GitHub create PR -> {cstatus}: {cbody[:300]!r}")


def _urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes]):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b""
    except urllib.error.URLError as exc:
        raise PancherryPublishError(f"GitHub network error: {exc.reason}") from exc
