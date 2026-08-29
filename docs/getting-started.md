# Getting started

## Prerequisites
- Python 3.11, a virtualenv (`.venv/` is used here).
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
