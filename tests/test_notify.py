"""Tests for alerting/notify.py — fully offline via an injected transport."""

from __future__ import annotations

import os
import sys
from urllib.parse import parse_qs

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from alerting.notify import NotifyError, notify_safe, send_telegram
from config.settings import ConfigError


class _RecordingTransport:
    """Captures the POST instead of hitting the network."""

    def __init__(self, raise_exc: Exception | None = None):
        self.calls: list[tuple[str, bytes, int]] = []
        self._raise = raise_exc

    def __call__(self, url: str, data: bytes, timeout: int) -> None:
        self.calls.append((url, data, timeout))
        if self._raise is not None:
            raise self._raise


def test_send_telegram_builds_url_and_payload():
    transport = _RecordingTransport()
    send_telegram("hello world", token="123:ABC", chat_id="42", transport=transport)

    assert len(transport.calls) == 1
    url, data, _timeout = transport.calls[0]
    assert url == "https://api.telegram.org/bot123:ABC/sendMessage"

    form = parse_qs(data.decode("utf-8"))
    assert form["chat_id"] == ["42"]
    assert form["text"] == ["hello world"]


def test_send_telegram_raises_on_transport_failure():
    transport = _RecordingTransport(raise_exc=NotifyError("boom"))
    with pytest.raises(NotifyError):
        send_telegram("x", token="t", chat_id="c", transport=transport)


def test_send_telegram_uses_env_credentials(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "envtok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "envchat")
    transport = _RecordingTransport()

    send_telegram("hi", transport=transport)

    url, data, _ = transport.calls[0]
    assert "botenvtok/sendMessage" in url
    assert parse_qs(data.decode())["chat_id"] == ["envchat"]


def test_send_telegram_missing_config_raises(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(ConfigError):
        send_telegram("hi", transport=_RecordingTransport())


def test_notify_safe_swallows_transport_failure(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    transport = _RecordingTransport(raise_exc=NotifyError("down"))
    assert notify_safe("msg", transport=transport) is False


def test_notify_safe_swallows_missing_config(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify_safe("msg", transport=_RecordingTransport()) is False


def test_notify_safe_returns_true_on_success(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    transport = _RecordingTransport()
    assert notify_safe("msg", transport=transport) is True
