"""Project-wide settings — reads secrets from env vars or Secret Manager path.

All credentials are injected via environment variables. Nothing is hardcoded.
The function never raises on import — only raises when a required setting is
actually accessed and missing, so unused settings don't block startup.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _load_dotenv_if_exists() -> None:
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and k not in os.environ:
                os.environ[k] = v

_load_dotenv_if_exists()


class ConfigError(ValueError):
    """Raised when a required config value is missing or invalid."""


def _required(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise ConfigError(
            f"Required environment variable {key!r} is not set. "
            "Set it in your shell or .env file before running."
        )
    return val


def _optional(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# --------------------------------------------------------------------------- #
# Google Sheets
# --------------------------------------------------------------------------- #

def get_spreadsheet_id() -> str:
    """The Google Sheet ID (from the URL: /d/<ID>/edit).
    Set via PORTFOLIO_SPREADSHEET_ID environment variable."""
    return _required("PORTFOLIO_SPREADSHEET_ID")


def get_service_account_info() -> dict:
    """Load the Google service-account JSON.

    Checks two sources in order:
    1. GOOGLE_SERVICE_ACCOUNT_JSON — the JSON content directly (for Cloud Run
       secrets mounted as env vars or GitHub Actions secrets).
    2. GOOGLE_APPLICATION_CREDENTIALS — a file path (for local dev).

    Raises ConfigError if neither is set or the JSON is invalid.
    """
    json_content = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if json_content:
        try:
            return json.loads(json_content)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is set but is not valid JSON"
            ) from exc

    path_str = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if path_str:
        path = Path(path_str)
        if not path.exists():
            raise ConfigError(
                f"GOOGLE_APPLICATION_CREDENTIALS points to {path!r} which does not exist"
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"Service account file {path!r} is not valid JSON"
            ) from exc

    raise ConfigError(
        "No Google service account credentials found. Set either:\n"
        "  GOOGLE_SERVICE_ACCOUNT_JSON  (JSON string, for Cloud Run / CI)\n"
        "  GOOGLE_APPLICATION_CREDENTIALS  (path to JSON file, for local dev)"
    )


# --------------------------------------------------------------------------- #
# Telegram alerting
# --------------------------------------------------------------------------- #

def get_telegram_bot_token() -> str:
    return _required("TELEGRAM_BOT_TOKEN")


def get_telegram_chat_id() -> str:
    return _required("TELEGRAM_CHAT_ID")


# --------------------------------------------------------------------------- #
# Broker credentials
# --------------------------------------------------------------------------- #

def get_tiger_credentials() -> dict:
    """Return Tiger credential fields from env vars."""
    return {
        "tiger_id": _required("TIGER_ID"),
        "account": _required("TIGER_ACCOUNT"),
        "private_key": _required("TIGER_PRIVATE_KEY"),  # inline PEM or file path
    }

def get_longbridge_credentials() -> dict:
    """Return Longbridge credential fields from env vars."""
    return {
        "app_key": _required("LONGBRIDGE_APP_KEY"),
        "app_secret": _required("LONGBRIDGE_APP_SECRET"),
        "access_token": _required("LONGBRIDGE_ACCESS_TOKEN"),
    }


def get_moomoo_settings() -> dict:
    """Return MooMoo OpenD-gateway connection settings from env vars.

    These describe how to reach the OpenD sidecar, not broker secrets — the
    broker login lives inside OpenD (see opend/README.md). All optional with
    sensible defaults, so a missing var never blocks the other broker legs.
    """
    markets = _optional("MOOMOO_MARKETS", "US,HK")
    return {
        "host": _optional("MOOMOO_HOST", "127.0.0.1"),
        "port": int(_optional("MOOMOO_PORT", "11111")),
        "security_firm": _optional("MOOMOO_SECURITY_FIRM", "FUTUSG"),
        "trd_env": _optional("MOOMOO_TRD_ENV", "REAL"),
        "acc_id": int(_optional("MOOMOO_ACC_ID", "0")),
        "markets": tuple(m.strip().upper() for m in markets.split(",") if m.strip()),
    }
