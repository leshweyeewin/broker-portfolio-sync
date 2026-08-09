# Job container for the daily sync (step 9 — BUILD_SPEC.md §12, §13).
#
# This runs `run.py` once and exits — it is a Cloud Run *Job* (batch), not a
# service. Cloud Scheduler triggers it daily. The MooMoo leg additionally needs
# the OpenD gateway running as a sidecar (see opend/); Longbridge and Tiger talk
# to their cloud APIs directly and need no sidecar.
#
# No credentials are baked in — everything is injected at runtime via env vars /
# mounted secrets (§9). See DEPLOY.md.

FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1

# Install deps first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# `docker run <image>` -> one sync pass. Append flags e.g. `--seed` on first run.
ENTRYPOINT ["python", "run.py"]
