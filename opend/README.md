# MooMoo OpenD sidecar

MooMoo's `moomoo` SDK does **not** connect to MooMoo directly. It talks to
**OpenD**, a gateway process that holds the authenticated session and exposes a
local TCP API (default port `11111`). The `MooMooAdapter` connects to OpenD; it
never sees broker credentials — those live inside OpenD.

```
adapters/moomoo.py ──TCP 11111──▶ OpenD gateway ──▶ MooMoo servers
   (the sync job)                 (this sidecar)
```

## Why a sidecar (BUILD_SPEC.md §2)

OpenD is a long-running process, so in production it runs as a **sidecar
container** next to the sync job. On Cloud Run this is a multi-container service:
the job and OpenD share `localhost`, so the job connects with
`MOOMOO_HOST=127.0.0.1`. Locally, `docker-compose.yml` runs both and the job
reaches OpenD by service name (`MOOMOO_HOST=opend`).

The Longbridge + Tiger legs need no gateway, so the bootstrap path (GitHub
Actions cron, §2) can skip this entirely and run those two brokers first.

## Getting the OpenD binary

OpenD is proprietary and is **not** committed to this repo. Download the Linux
build from MooMoo's OpenAPI download page, then supply it to the image build in
one of two ways (see `Dockerfile`):

- **Build-arg URL** (CI-friendly): `docker build --build-arg OPEND_URL=<tarball-url> -t opend-sidecar opend/`
- **Vendored file**: place the tarball at `opend/vendor/OpenD.tar.gz` (this path
  is gitignored) and build normally.

## Credentials (never baked into the image)

Supplied at runtime as env vars (from Secret Manager / GitHub secrets):

| Var | Meaning |
|-----|---------|
| `MOOMOO_LOGIN_ACCOUNT` | phone/email used to log in to OpenD |
| `MOOMOO_LOGIN_PWD_MD5` | **MD5** of the trade-unlock password — never plaintext |
| `MOOMOO_API_PORT` | gateway port (default `11111`) |
| `MOOMOO_API_IP` | bind address (default `0.0.0.0` so the job can reach it) |

Compute the MD5 locally, e.g.:

```bash
printf '%s' 'your-unlock-password' | md5sum
```

> Queries used by the adapter are read-only, so the SDK side needs no
> `unlock_trade` call. OpenD still logs in with the account above to hold the
> session.

## Adapter-side settings

The job configures the adapter via `MooMooCredentials` / env (`MOOMOO_` prefix):
`MOOMOO_HOST`, `MOOMOO_PORT`, `MOOMOO_SECURITY_FIRM` (`FUTUSG` for the Singapore
account), `MOOMOO_TRD_ENV` (`REAL`), `MOOMOO_ACC_ID` (`0` = first account),
`MOOMOO_MARKETS` (e.g. `US,HK`).

## Run locally

```bash
docker compose -f opend/docker-compose.yml up --build
```
(Requires a `.env` with the login vars and either `OPEND_URL` or a vendored tarball.)
