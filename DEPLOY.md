# DEPLOY — running the daily sync unattended (step 9)

The pipeline is one batch entrypoint, [`run.py`](run.py): it fetches every
configured broker, computes FIFO P/L, converts with trade-date FX, writes the
Google Sheet idempotently, reconciles against live positions, appends a Run Log
row, and alerts on Telegram if anything is off. Run it once a day.

Two ways to schedule it, cheapest first:

| Option | Brokers it covers | Why |
|--------|-------------------|-----|
| **A. GitHub Actions cron** (bootstrap) | Longbridge + Tiger | No gateway needed — pure cloud APIs. Free, zero infra. |
| **B. Cloud Run Job + Scheduler** | All three (incl. MooMoo) | MooMoo needs the OpenD gateway as a sidecar — see [`opend/`](opend/README.md). |

Start with A while validating against real accounts; move to B when you want
MooMoo in the mix.

---

## Secrets (both options)

Nothing is ever committed (§9). `run.py` reads everything from env / mounted
secrets via [`config/settings.py`](config/settings.py):

| Variable | Purpose |
|----------|---------|
| `PORTFOLIO_SPREADSHEET_ID` | Target sheet (`…/d/<ID>/edit`). Share it with the service-account email as **Editor**. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service-account key JSON (string). *(Local dev may use `GOOGLE_APPLICATION_CREDENTIALS` = file path instead.)* |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alerting. Alerts are best-effort — a missing/broken bot never sinks the run, it just logs. |
| `TIGER_ID`, `TIGER_ACCOUNT`, `TIGER_PRIVATE_KEY` | Tiger. |
| `LONGBRIDGE_APP_KEY`, `LONGBRIDGE_APP_SECRET`, `LONGBRIDGE_ACCESS_TOKEN` | Longbridge. |
| `MOOMOO_HOST`, `MOOMOO_PORT`, `MOOMOO_MARKETS`, … | How to reach the OpenD sidecar (not broker secrets — those live inside OpenD; see [`opend/README.md`](opend/README.md)). |

A broker whose credentials are absent is **skipped with a warning**, not an
error — so option A can run with only Tiger + Longbridge set.

---

## First run: seeding

If a broker's API does not return your full lifetime history, run **once** with
`--seed` to synthesize Opening Balance rows from current positions (stable dedup
keys → re-running `--seed` upserts, never duplicates):

```bash
python run.py --seed
```

Thereafter run without the flag. (See HANDOFF.md for the seeding-lifecycle
caveat if a broker only returns partial history.)

---

## Option A — GitHub Actions cron

`.github/workflows/sync.yml` (create it; secrets go in repo **Settings →
Secrets and variables → Actions**):

```yaml
name: portfolio-sync
on:
  schedule:
    - cron: "30 21 * * *"   # 05:30 SGT daily (UTC+8) — after US close
  workflow_dispatch: {}       # manual "Run workflow" button
jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r requirements.txt
      - run: python run.py
        env:
          PORTFOLIO_SPREADSHEET_ID: ${{ secrets.PORTFOLIO_SPREADSHEET_ID }}
          GOOGLE_SERVICE_ACCOUNT_JSON: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          TIGER_ID: ${{ secrets.TIGER_ID }}
          TIGER_ACCOUNT: ${{ secrets.TIGER_ACCOUNT }}
          TIGER_PRIVATE_KEY: ${{ secrets.TIGER_PRIVATE_KEY }}
          LONGBRIDGE_APP_KEY: ${{ secrets.LONGBRIDGE_APP_KEY }}
          LONGBRIDGE_APP_SECRET: ${{ secrets.LONGBRIDGE_APP_SECRET }}
          LONGBRIDGE_ACCESS_TOKEN: ${{ secrets.LONGBRIDGE_ACCESS_TOKEN }}
```

`run.py` exits non-zero only if **every** broker fails, so the Actions run goes
red on a total failure and stays green on a normal (or PARTIAL) run — the Run
Log tab and the Telegram alert carry the detail.

---

## Option B — Cloud Run Job + Cloud Scheduler

Build and push the job image (Dockerfile at repo root):

```bash
PROJECT=your-gcp-project
REGION=asia-southeast1
IMAGE=$REGION-docker.pkg.dev/$PROJECT/portfolio/sync:latest

gcloud builds submit --tag "$IMAGE"
```

Put secrets in Secret Manager, then create the job (secrets mounted as env vars):

```bash
gcloud run jobs create portfolio-sync \
  --image "$IMAGE" --region "$REGION" \
  --set-secrets "PORTFOLIO_SPREADSHEET_ID=portfolio-spreadsheet-id:latest,\
GOOGLE_SERVICE_ACCOUNT_JSON=google-sa-json:latest,\
TELEGRAM_BOT_TOKEN=telegram-bot-token:latest,\
TELEGRAM_CHAT_ID=telegram-chat-id:latest,\
TIGER_ID=tiger-id:latest,TIGER_ACCOUNT=tiger-account:latest,TIGER_PRIVATE_KEY=tiger-private-key:latest,\
LONGBRIDGE_APP_KEY=lb-app-key:latest,LONGBRIDGE_APP_SECRET=lb-app-secret:latest,LONGBRIDGE_ACCESS_TOKEN=lb-access-token:latest"
```

Schedule it daily:

```bash
gcloud scheduler jobs create http portfolio-sync-daily \
  --location "$REGION" --schedule "30 21 * * *" --time-zone "UTC" \
  --uri "https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/portfolio-sync:run" \
  --http-method POST \
  --oauth-service-account-email "scheduler@$PROJECT.iam.gserviceaccount.com"
```

Run the first (seeding) pass manually:

```bash
gcloud run jobs execute portfolio-sync --region "$REGION" --args=--seed
```

### MooMoo: add the OpenD sidecar

The MooMoo SDK talks to the **OpenD gateway**, not to MooMoo directly. Deploy
OpenD as a second container in the same Cloud Run job and point the job at it
with `MOOMOO_HOST=localhost` (sidecars share a network namespace). The gateway
image, entrypoint, and the local `docker-compose.yml` for testing the pairing
are all in [`opend/`](opend/README.md) — including how the proprietary OpenD
binary is fetched at build time (never committed) and how the MooMoo login is
injected into OpenD via env vars.

---

## What a run leaves behind

- **Stocks / Options / Transactions tabs** — upserted on `_dedup_key`; re-runs
  never duplicate.
- **Dashboard tab** — overwritten: per-broker realized P/L (SGD), open-position
  counts, run status, reconciliation result, last-run timestamp.
- **Run Log tab** — one appended row per run: timestamp, status
  (`OK`/`PARTIAL`/`FAILED`), rows added/updated per tab, FX rates used,
  reconciliation result, and the full warning list.
- **Telegram** — a message only when status ≠ OK or reconciliation flags a
  mismatch. A clean run is silent.
