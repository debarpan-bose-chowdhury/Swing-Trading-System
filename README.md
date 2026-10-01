# Swing-Trading-System

A Python 3.12+ service for the swing-trading system. The current implementation
provides an HTTPS health endpoint, an NSE ticker-metadata pipeline, the Ticker Data System (OHLCV price history) and
the Stock Analyst (weekly target list; the broker Ledger is still to come).

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

Run the tests (unit tests cover every stage; the NSE and Yahoo endpoints are never contacted; needs the dependencies from
`pyproject.toml`, e.g. `pip install -e . pytest pytest-cov`):

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

### Ticker Data System (price history)

Maintains daily OHLCV history for every ticker the metadata pipeline has ever selected, plus the indices in
`app/config/indices.json` (`^NSEI`, `^NSEBANK`, `^BSESN`). Design: *Ticker Data System - TDD*. Data comes from `yfinance`
(raw prices, `auto_adjust=False`, plus `AdjClose`), is validated row by row, and is stored per ticker in two tiers:
recent rows as CSV, rows older than 365 days as Parquet + zstd.

| Stage | Command | When | What it does |
|---|---|---|---|
| Migrator | `python -m app.market.migrator` | Manual, once | Needs today's healthy `health.json`. Seeds `registry.csv` from the upstream bucket files, downloads max history (50 tickers per batch, indices one at a time), splits at the cutoff. Resumable through `migration_checkpoint.json`; writes `migration.done` when every ticker and index is settled. |
| Updator | `python -m app.market.updator` | Daily 21:00 IST | Refreshes the registry (only if upstream is healthy *today*), backfills new tickers, gap-fills new trading days plus a 30-day lookback, rebuilds a ticker's full history on a split/dividend or when Yahoo restates stored prices, sets a ticker inactive after 5 consecutive no-data trading days. |
| Archiver | `python -m app.market.archiver` | 22:00 IST on the 2nd of Jan/Apr/Jul/Oct | Moves rows older than 365 days from the fresh CSV to a Parquet partition (write temp, verify row count and keys, rename, then trim the CSV). |

Storage (`app/data/market/`): `registry.csv`, `fresh/{Ticker}.csv`, `archive/{Ticker}/Compressed_{First}_{Last}.parquet`,
`indices/{fresh,archive}/...`, `status.json`, `migration.done`, `.lock`. Schema for CSV and Parquet:
`Ticker, Date, Open, High, Low, Close, AdjClose, Volume`; `(Ticker, Date)` is unique.

Behaviour worth knowing:

- **Validator** (every stage): `SCHEMA`, `NULL_VALUE` (refetched up to 2 passes, then rejected), `OHLC`, `DUP_IN_BATCH`,
  `NON_TRADING_DAY`. Valid rows commit, bad rows go to `app/data/logs/rejects/<date>_<stage>.csv`; a ticker is rolled back
  entirely when more than 20% of its returned rows are rejected or a write fails.
- **Throttle**: 15 s between calls, 2,000 requests/hour, 3 retries (2/4/8 s + jitter). On a rate limit (429/999) the run
  sleeps 60 min and resumes, at most 3 times; the rest is left for the next day. yfinance hides per-ticker rate-limit
  errors in its log, so blocks are detected from that log as well as from raised exceptions.
- **Only final bars are stored**: a session counts as final after 20:00 IST (`sessionFinalAfterIST`).
- **Run lock** (`.lock`, stale after 6 h): only one stage touches the data at a time; a second stage exits with code 2.
  All writes are temp file + atomic rename.
- **Every run** writes `status.json` (read it first if you consume the data; a `.lock` file means a run is in progress)
  and sends one digest email. A failed or unconfigured email never fails the run.
- Dates and cut-offs are IST (fixed UTC+5:30); the metadata stages still use the host's local date.

Interpretations of points the TDD leaves open (agreed during implementation):

- **Re-activation**: an inactive ticker is re-activated only if it was absent from at least one healthy registry refresh
  after going inactive and is listed upstream again (extra registry column `absent_since_inactive`). A dead ticker that
  simply stays in the upstream list stays inactive.
- **Restatement check**: each incremental fetch starts at the last stored date (one day of overlap); a differing OHLC on
  that day (tolerance 0.01%) triggers the same full rebuild as a split.
- **No-data counting** only happens when the most recent trading day is missing for that ticker, and is skipped for the
  whole run if no ticker at all returned new rows (an outage, not dead tickers). A failed or deferred fetch never counts.
- **Migrator** treats a ticker for which Yahoo returns nothing as settled, so it cannot block completion; the Updator then
  keeps trying it and applies the 5-day rule.
- **Calendar**: `app/config/nse_calendar.json` ships with empty `holidays` / `specialSessions`; weekends are always
  handled. Fill it from NSE's yearly holiday circular, otherwise a holiday looks like a missing trading day (it is
  re-checked daily for 30 days and is never stored).

Configuration: `app/config/market.json` (paths, throttle, retries, thresholds, SMTP host/sender/recipients). SMTP credentials come only from the `SMTP_USER` / `SMTP_PASSWORD`
environment variables. Copy `market.json`, `indices.json` and `nse_calendar.json` into the host config folder and set the
`<set at deployment>` mail values.

#### Windows Task Scheduler (market stages)

Use the separate market image (`Dockerfile.market`, built by CI as `swing-trading-market`) and the same bind mounts as the
metadata pipeline:

```powershell
docker run --rm `
  -v C:\ProgramData\ticker-pipeline\data:/app/app/data `
  -v C:\ProgramData\ticker-pipeline\config:/app/app/config `
  -e SMTP_USER -e SMTP_PASSWORD `
  ghcr.io/debarpan-bose-chowdhury/swing-trading-market:latest python -m app.market.updator
```

Run `python -m app.market.migrator` by hand once after the first healthy upstream run. The Updator (default command of the
image) runs daily at 21:00 IST, after the upstream Data Source/Filter/Notifier; the Archiver runs 22:00 IST on the 2nd of
Jan/Apr/Jul/Oct (Task Scheduler has no single "2nd of these months" trigger; use one monthly trigger on day 2 with those
four months selected). The 2nd never coincides with the upstream Cleaner on the 1st.

### Stock Analyst (weekly targets)

Design: `app/doc/Stock_Analyst_TDD.md` (read its "As-built decisions" first). It reads the Ticker Data prices, the
Metadata bucket files and the Ledger's book, and writes `app/data/analyst/targets/targets_{rebalance_date}.json` for the
Risk Manager. It places no orders. Phase 1 (Signals) is built; the Ledger and the broker probe come next.

| Stage | Command | When | What it does |
|---|---|---|---|
| Signals | `python -m app.analyst.signals` | Friday 21:30 IST, repeated hourly until Sunday 22:00 | Gate (Ticker Data `status.json` ok/partial for the rebalance date, no `market/.lock`, index row present, holidays loaded, `placeholders` false), regime from the `^NSEI` close (SMA50/SMA200/ROC63, weekly persistence, BEAR immediate), per-bucket selection (liquidity, full-window trend MA, momentum, BEAR composite ranking), delta against `ledger/book.csv` (KEEP/ADD/DROP, omitted when the book is missing or older than 3 days), cost estimates, target file |
| Replay | `python -m app.analyst.signals --as-of 2026-09-25` | Manual | Read-only: prints the target JSON for that rebalance date and writes nothing |

Every stage accepts `--check` (config and imports only). Exit codes: 0 ok, 1 failed, 2 busy (run lock), 3 gate not met
(the hourly retry tries again; no email until the final Sunday attempt). A target file is never edited; a rerun for the
same date is a no-op unless `--force`, which renames the old file `targets_{date}.superseded_{time}.json`.

Configuration: `app/config/analyst.json`. It ships with the strategy repository's numbers and `"placeholders": true`, which
makes Signals refuse to run: confirm the strategy values, composition and floating capital, then set it to `false`.
`app/config/seed_positions.csv` (header only) is for the Ledger.

**Secrets** (Angel One and SMTP) are environment variables, never files in the repo, data or config folders. Copy
`.env.example` to `C:\ProgramData\ticker-pipeline\secrets\analyst.env` (outside the mounted folders), fill it in and
restrict it to your Windows user. `.env` and `*.env` are git- and docker-ignored. Task Scheduler passes the file:

```powershell
docker run --rm `
  -v C:\ProgramData\ticker-pipeline\data:/app/app/data `
  -v C:\ProgramData\ticker-pipeline\config:/app/app/config `
  --env-file C:\ProgramData\ticker-pipeline\secrets\analyst.env `
  ghcr.io/debarpan-bose-chowdhury/swing-trading-analyst:latest python -m app.analyst.signals
```

Signals only needs the SMTP variables; a value still written as `<set at deployment>` counts as unset. The Task Scheduler
trigger is Friday 21:30 repeated every 60 minutes for 48.5 hours, without starting a new instance while one runs.

## Container

`Dockerfile` builds the HTTPS service image; `Dockerfile.market` builds the Ticker Data System image (adds
`yfinance`, `pandas`, `pyarrow`; default command `python -m app.market.updator`); `Dockerfile.analyst` builds the Stock Analyst
image (default command `python -m app.analyst.signals`). The service image starts the HTTPS service. Mount a directory containing `cert.pem` and
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

Set `APP_PORT` to change the host port, or `IMAGE_REF_SWING_TRADING_SYSTEM` (or `IMAGE_REF`) to select a different
image. The staging configuration uses the same settings with fewer health-check
retries.

## Project layout

- `app/__main__.py`: HTTPS server and `/health` endpoint.
- `app/metadata/`: ticker-metadata pipeline (`data_source`, `filter`, `cleaner`, `notifier`, shared `common`).
- `app/market/`: Ticker Data System (`migrator`, `updator`, `archiver`; shared `validator`, `fetcher`, `store`, `registry`, `ingest`, `tradingcal`, `mailer`, `common`).
- `app/analyst/`: Stock Analyst (`signals`, `regime`, `selector`, `costs`, `secrets`, shared `common`); the Ledger and probe are phase 2.
- `app/config/`: `config.json` (metadata pipeline), `market.json`, `indices.json`, `nse_calendar.json` (market stages), `analyst.json`, `seed_positions.csv` (analyst).
- `app/data/`: pipeline output (`raw/`, `storage/`, `market/`, `logs/`, `health.json`); git-ignored.
- `tests/`: unit tests for the HTTP service, the metadata pipeline and the market stages (Yahoo is always faked).

## CI/CD

`.github/workflows/ci-cd.yml` only calls the reusable workflows in
[`debarpan-bose-chowdhury/CI-CD`](https://github.com/debarpan-bose-chowdhury/CI-CD) (CI + CodeQL + SBOM, multi-image
Docker build/push to GHCR, staging deploy, smoke + OWASP ZAP DAST, production deploy). Same-repo pull requests also build,
stage and scan (without moving `:latest`); only pushes to `main` deploy to production. `build.yml` runs the tests and
the SonarQube scan. Required secrets: `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`, `SONAR_TOKEN`; configure the
`staging` and `production` GitHub environments before enabling deployments. All pipeline logic is shared and lives in
CI-CD; this repo only supplies the inputs (the `images` list, compose files, HTTPS health URL, `tls-cert: true`).

Two images are built in one `build-docker` job (an `images` list passed to the shared workflow):

| Image | Dockerfile | Role | Pipeline |
|---|---|---|---|
| `swing-trading-system` | `Dockerfile` | `service` | build, staging deploy, DAST, production deploy |
| `swing-trading-market` | `Dockerfile.market` | `batch` | build, smoke-run, weekly scan (runs from Windows Task Scheduler, never deployed) |
| `swing-trading-analyst` | `Dockerfile.analyst` | `batch` | same as the market image (smoke-run `python -m app.analyst.signals --check`) |

- **Smoke-run:** after the push, CI runs `docker run <market image> python -m app.market.updator --check`. `--check`
  (available on every market and metadata stage) loads the config (and the trading calendar), proves the dependencies
  import, prints `<stage>: check ok` and exits 0, or exits 1 on a config error. It contacts no network and writes no data,
  logs or lock files.
- **Compose:** the deploy exports `IMAGE_REF_SWING_TRADING_SYSTEM` (name upper-cased, `-` becomes `_`); the compose files
  read it and fall back to `IMAGE_REF`, then `:latest`.
- **Scan:** `image-scan.yml` scans the images weekly (Mondays 03:17 UTC) and on demand (`workflow_dispatch`).
- **Adding an image:** add an entry (`name`, `language`, `dockerfile`, `role`, ...) to `images` in `ci-cd.yml`; for a
  service also give it a compose service using `IMAGE_REF_<NAME>` and a `health-url`. See the CI-CD README section
  "Multi-image pipelines".
