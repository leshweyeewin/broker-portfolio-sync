#!/usr/bin/env bash
# Entrypoint for the MooMoo OpenD gateway sidecar.
#
# OpenD is MooMoo/Futu's session gateway: the `moomoo` SDK connects to it over
# TCP (default 11111) rather than to MooMoo directly. This script builds OpenD's
# command line from environment variables so NO credentials are baked into the
# image (§9 secrets hygiene). In production these env vars come from Secret
# Manager (Cloud Run) or GitHub Actions secrets.
#
# Required env:
#   MOOMOO_LOGIN_ACCOUNT   phone/email used to log in to OpenD
#   MOOMOO_LOGIN_PWD_MD5   MD5 of the *trade unlock* password (NOT plaintext)
# Optional env:
#   MOOMOO_API_PORT        default 11111
#   MOOMOO_API_IP          default 0.0.0.0 (so the job container can reach it)
#   MOOMOO_LANG            default en
#   MOOMOO_LOG_LEVEL       default no   (no | info | warning | error)
#   OPEND_BIN              default /opt/opend/OpenD
set -euo pipefail

: "${MOOMOO_LOGIN_ACCOUNT:?set MOOMOO_LOGIN_ACCOUNT}"
: "${MOOMOO_LOGIN_PWD_MD5:?set MOOMOO_LOGIN_PWD_MD5 (md5 of the unlock password, never plaintext)}"

OPEND_BIN="${OPEND_BIN:-/opt/opend/OpenD}"
API_PORT="${MOOMOO_API_PORT:-11111}"
API_IP="${MOOMOO_API_IP:-0.0.0.0}"
LANG_OPT="${MOOMOO_LANG:-en}"
LOG_LEVEL="${MOOMOO_LOG_LEVEL:-no}"

if [[ ! -x "$OPEND_BIN" ]]; then
  echo "OpenD binary not found/executable at $OPEND_BIN. See opend/README.md — " \
       "the binary must be fetched into the image at build time." >&2
  exit 1
fi

echo "Starting OpenD on ${API_IP}:${API_PORT} (lang=${LANG_OPT}, log=${LOG_LEVEL})"
exec "$OPEND_BIN" \
  -login_account="${MOOMOO_LOGIN_ACCOUNT}" \
  -login_pwd_md5="${MOOMOO_LOGIN_PWD_MD5}" \
  -ip="${API_IP}" \
  -api_port="${API_PORT}" \
  -lang="${LANG_OPT}" \
  -log_level="${LOG_LEVEL}"
