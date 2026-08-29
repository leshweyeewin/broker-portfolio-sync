# Getting started

## Prerequisites
- Python 3.12+ (tested on 3.14), a virtualenv (`.venv/` is used here).
- Broker credentials + a Google Cloud service account — **user-provided** (§13),
  never committed. See the checklist below.

## Install & test
```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe -m pytest -q
```

> **Windows note:** set `PYTHONUTF8=1` before scripts that print SDK objects, or
> cp1252 will crash on non-ASCII output.

> **Broker SDK tests:** `test_tiger.py`, `test_longbridge.py`, and `test_moomoo.py`
> require their respective broker SDKs (`tigeropen`, `longport`, `moomoo-api`).
> They are automatically skipped if the SDK is not installed. To run only the
> offline tests: `pytest tests/ --ignore=tests/test_longbridge.py --ignore=tests/test_moomoo.py --ignore=tests/test_tiger.py`

## Environment variables

All credentials are read from environment variables (or `.env`). See
[`config/settings.py`](../config/settings.py) for the full list. The minimum
set to run the daily sync:

| Variable | Required for | Example |
|---|---|---|
| `PORTFOLIO_SPREADSHEET_ID` | Sheet sync | `1abc...xyz` (from the Sheet URL) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Sheet sync | `{"type":"service_account",...}` |
| `TIGER_ID` / `TIGER_ACCOUNT` / `TIGER_PRIVATE_KEY` | Tiger broker | developer portal |
| `LONGBRIDGE_APP_KEY` / `LONGBRIDGE_APP_SECRET` / `LONGBRIDGE_ACCESS_TOKEN` | Longbridge broker | developer portal |
| `MOOMOO_HOST` / `MOOMOO_PORT` | MooMoo (via OpenD) | `127.0.0.1` / `11111` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Alerts | BotFather token + chat ID |

## User setup checklist (cannot be done by the pipeline — §13)
- [ ] **Longbridge**: developer verification → token (open.longbridge.com)
- [ ] **Tiger**: RSA keypair + `tiger_id` + account, `license='TBSG'` (developer.itigerup.com)
- [ ] **MooMoo**: Futu/moomoo ID for OpenD login; decide OpenD host (sidecar)
- [ ] **Google Cloud**: service account + JSON key
- [ ] Share the new Sheet with the service-account email (**Editor**)
- [ ] Put all secrets in Secret Manager (or GitHub secrets)
- [ ] Confirm each account's traded currencies (for FX pairs)
- [ ] Choose alert channel (Telegram/email) + provide its credential

For deployment (GitHub Actions cron / Cloud Run Job + Scheduler), see
[`DEPLOY.md`](../DEPLOY.md).
