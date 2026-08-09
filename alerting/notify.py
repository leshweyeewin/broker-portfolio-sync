"""Telegram alerting (step 9 — BUILD_SPEC.md §9).

The daily job runs unattended, so a failure or a reconciliation mismatch has to
reach the user out-of-band. Telegram is the chosen channel (the user's
preference): a bot token + chat id, one HTTPS POST, no SDK.

Design
------
* No third-party dependency — plain ``urllib`` (same as ``core/fx.py``), so the
  job container stays slim and there is nothing extra to pin.
* **Fail loud** (§9): a send that the API rejects raises ``NotifyError``. The
  *caller* (``run.py``) decides whether a failed alert should sink the whole run
  — alerting is best-effort there, but this module never swallows errors
  silently, so a misconfigured bot is visible in the logs.
* Testable offline: the HTTP call goes through an injectable ``transport``
  callable, so tests assert on the URL/payload without touching the network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Optional
from urllib.parse import urlencode

from config.settings import ConfigError, get_telegram_bot_token, get_telegram_chat_id

_API_BASE = "https://api.telegram.org"
_REQUEST_TIMEOUT = 10

# A transport takes (url, form-encoded body bytes, timeout seconds) and performs
# the POST, raising NotifyError on any non-success. Injectable for tests.
Transport = Callable[[str, bytes, int], None]


class NotifyError(RuntimeError):
    """Raised when an alert cannot be delivered (network, API error, config)."""


def _urllib_post(url: str, data: bytes, timeout: int) -> None:
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "broker-portfolio-sync/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if exc.fp else exc.reason
        raise NotifyError(f"Telegram HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise NotifyError(f"Telegram network error: {exc.reason}") from exc
    except OSError as exc:
        raise NotifyError(f"Telegram OS error: {exc}") from exc

    # Telegram always returns JSON {"ok": bool, ...}; ok=false is a logical error.
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise NotifyError(f"Telegram returned non-JSON: {body!r}") from exc
    if not payload.get("ok", False):
        raise NotifyError(f"Telegram rejected the message: {payload}")


def send_telegram(
    text: str,
    *,
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
    timeout: int = _REQUEST_TIMEOUT,
    transport: Optional[Transport] = None,
) -> None:
    """Send ``text`` to the configured Telegram chat.

    ``token``/``chat_id`` default to ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``
    from the environment (via ``config.settings``). Raises ``NotifyError`` on any
    delivery failure, ``ConfigError`` if credentials are missing.
    """
    token = token or get_telegram_bot_token()
    chat_id = chat_id or get_telegram_chat_id()
    transport = transport or _urllib_post

    url = f"{_API_BASE}/bot{token}/sendMessage"
    data = urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    transport(url, data, timeout)


def notify_safe(
    text: str,
    *,
    transport: Optional[Transport] = None,
) -> bool:
    """Best-effort send: never raises. Returns True if delivered.

    Use this from the run entrypoint so a broken/absent Telegram config can never
    turn a successful sync into a crashed job. The failure is still surfaced —
    it's returned as False and logged by the caller — just not raised.
    """
    try:
        send_telegram(text, transport=transport)
        return True
    except (NotifyError, ConfigError):
        return False
