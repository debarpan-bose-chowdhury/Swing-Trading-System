# Swing-Trading-System

A Python 3.12+ service for the swing-trading system. The current implementation
provides an HTTPS health endpoint and an NSE ticker-metadata pipeline.

## Development

### HTTPS health service

The service listens on port `8080` by default and requires both
`TLS_CERTFILE` and `TLS_KEYFILE`. Create or provide a certificate and key, then
start the service:

```bash
export TLS_CERTFILE=certs/cert.pem
export TLS_KEYFILE=certs/key.pem
python -m app
```

On PowerShell, use `$env:TLS_CERTFILE` and `$env:TLS_KEYFILE`. On Windows CMD, use `set TLS_CERTFILE=certs\cert.pem`.
The port can be changed with `PORT`.

Check the health endpoint over HTTPS:

```bash
curl -k https://localhost:8080/health
```

The endpoint returns `{"status": "ok"}`. Unknown paths return `404`.

### Ticker-metadata pipeline

A daily, health-gated batch pipeline (design: *NSE Ticker Metadata Pipeline - TDD*) that builds
rolling top-N ticker lists per market-cap bucket. Four stages, each a separate command:

| Stage | Command | What it does |
|---|---|---|
| Data Source | `python -m app.metadata.data_source` | Fetches NSE `EQUITY_L.csv` (listing dates) and the Bhavcopy (PR) `mcap` file (market caps) using a cookie session and 3 retries (2s/4s/8s), inner-joins on symbol (unmatched symbols are logged and dropped) and writes `app/data/raw/<date>.csv` (`Symbol,MarketCap,InceptionDate`). Exits non-zero if the fetch never succeeds. |
| Filter | `python -m app.metadata.filter` | Drops tickers listed less than `minInceptionDays` (365) ago, assigns each ticker to the highest bucket whose `minMarketCap` it meets, keeps the top `topN` per bucket (ties: symbol ascending) and writes `app/data/storage/<Bucket>_<date>.csv`. |
| Cleaner | `python -m app.metadata.cleaner` | Quarterly: keeps only the newest dated file per bucket in `storage/` and in `raw/` (by filename date). Skipped unless `health.json` is healthy. |
| Notifier | `python -m app.metadata.notifier` | Checks that today's raw file and one file per bucket exist and writes `app/data/health.json` (`status`, `checkedAt`, `missing`). Notifying the downstream Ticker Data System / Artifact Handler is out of scope and is logged only. |

Order: daily `Data Source -> Filter -> Notifier`; quarterly (1st of Jan/Apr/Jul/Oct) `Cleaner -> Notifier`.
Every stage logs to stdout and `app/data/logs/<stage>_<date>.log`. The date is the host's local date.

Health gating decisions (deviations from / clarifications of the TDD):

- **Cleaner** is gated strictly: anything other than `"status": "healthy"` (including a corrupt file) skips deletion.
- **Filter** is *not* blocked by an unhealthy flag; it logs a warning and runs. Blocking it would prevent it from
  ever writing the files Notifier needs, so a single failed day could never recover. It is non-destructive.
- A missing `health.json` counts as healthy so the very first run can bootstrap.

Configuration lives in `app/config/config.json` (bucket thresholds/`topN`, paths, NSE URLs, retry policy,
schedule). Buckets are evaluated highest threshold first regardless of list order, and any number of buckets is
allowed. Set `CONFIG_PATH` to use a different file. The thresholds shipped are the TDD placeholders
(2T / 500B / 100B INR, N = 50) and still need to be tuned.

Run the tests (unit tests cover every stage; the NSE endpoints are never contacted):

```bash
python -m unittest discover -s tests -v   # or: pytest
```

#### Windows Task Scheduler (one task per stage)

The image's default command is the HTTPS health service; the stages run by overriding the command. Mount the host
folders over the config-relative paths `app/data` and `app/config` (copy `app/config/config.json` into the host
config folder first):

```powershell
docker run --rm `
  -v C:\ProgramData\ticker-pipeline\data:/app/app/data `
  -v C:\ProgramData\ticker-pipeline\config:/app/app/config `
  <image> python -m app.metadata.data_source   # then .filter, .notifier (daily) / .cleaner, .notifier (quarterly)
```

Stagger the daily tasks a few minutes apart from 19:30 (after NSE close) so they never overlap; the design relies on
that instead of file locks. The bind-mount path `C:\ProgramData\ticker-pipeline` is a default to confirm.

## Container

The image starts the HTTPS service. Mount a directory containing `cert.pem` and
`key.pem`, and pass their paths into the container:

```bash
docker build -t swing-trading-system .
docker run --rm -p 8080:8080 \
	-e TLS_CERTFILE=/certs/cert.pem \
	-e TLS_KEYFILE=/certs/key.pem \
	-v "${PWD}/certs:/certs:ro" \
	swing-trading-system
```

For PowerShell, `${PWD}` resolves to the current directory. The repository also
includes Compose configurations that mount `./certs` and define a TLS-aware
health check:

```bash
docker compose up
```

Set `APP_PORT` to change the host port, or `IMAGE_REF` to select a different
image. The staging configuration uses the same settings with fewer health-check
retries.

## Project layout

- `app/__main__.py`: HTTPS server and `/health` endpoint.
- `app/metadata/`: ticker-metadata pipeline (`data_source`, `filter`, `cleaner`, `notifier`, shared `common`).
- `app/config/config.json`: pipeline configuration.
- `app/data/`: pipeline output (`raw/`, `storage/`, `logs/`, `health.json`); git-ignored.
- `tests/`: unit tests for the HTTP service and the metadata pipeline.

## CI/CD

`.github/workflows/ci-cd.yml` only calls the reusable workflows in
[`debarpan-bose-chowdhury/CI-CD`](https://github.com/debarpan-bose-chowdhury/CI-CD) (CI + CodeQL + SBOM, Docker
build/push to GHCR, staging deploy, smoke + OWASP ZAP DAST, production deploy). Same-repo pull requests also build,
stage and scan (without moving `:latest`); only pushes to `main` deploy to production. `build.yml` runs the tests and
the SonarQube scan. Required secrets: `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`, `SONAR_TOKEN`; configure the
`staging` and `production` GitHub environments before enabling deployments. All pipeline logic is shared and lives in
CI-CD; this repo only supplies the inputs (image name, compose files, HTTPS health URL, `tls-cert: true`).
