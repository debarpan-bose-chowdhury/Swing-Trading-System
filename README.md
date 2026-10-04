# Swing-Trading-System

A Python 3.12+ service for the swing-trading system. The current implementation
provides an HTTPS health endpoint, an NSE ticker-metadata pipeline, the Ticker Data System (OHLCV price history) and
the Stock Analyst (broker ledger, trading journal and weekly target list) and the Risk Manager (daily and weekly buy/sell signals).

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

Design: `doc/Stock_Analyst_TDD.md` (read its "As-built decisions" first). It reads the Ticker Data prices, the
Metadata bucket files and the Ledger's book, and writes `app/data/analyst/targets/targets_{rebalance_date}.json` for the
Risk Manager. It places no orders. Stages: Ledger (broker -> fills, book, journal), Signals (targets) and the broker Probe.

| Stage | Command | When | What it does |
|---|---|---|---|
| Signals | `python -m app.analyst.signals` | Friday 21:30 IST, repeated hourly until Sunday 22:00 | Gate (Ticker Data `status.json` ok/partial for the rebalance date, no `market/.lock`, index row present, holidays loaded, `placeholders` false), regime from the `^NSEI` close (SMA50/SMA200/ROC63, weekly persistence, BEAR immediate), per-bucket selection (liquidity, full-window trend MA, momentum, BEAR composite ranking), delta against `ledger/book.csv` (KEEP/ADD/DROP, omitted when the book is missing or older than 3 days), cost estimates, target file |
| Ledger | `python -m app.analyst.ledger` | Mon-Fri 16:30 IST, hourly retry until 20:30 | Logs in to Angel One (client code, MPIN, TOTP from the env-file), snapshots tradebook / positions / holdings / funds, appends today's NSE DELIVERY fills of tracked tickers to `ledger/fills.csv`, replays the open-position book `ledger/book.csv` (average cost) and appends closed sales to `trading_journal.csv` (P&L, estimated charges). Reconciles against holdings: corporate action (within 1% of cost), missed buy, missed sell (ESTIMATED journal row you correct later). Seeds from `seed_positions.csv` on the first run, then keeps 30-day backups and purges snapshots after 90 days. Skips non-trading days and a day that already succeeded |
| Probe | `python -m app.analyst.probe --check-broker` | Manual, before go-live and after any Angel One change | Logs in and prints the field names and value types (never values) each read endpoint returns, and flags missing expected fields |
| Replay | `python -m app.analyst.signals --as-of 2026-09-25` | Manual | Read-only: prints the target JSON for that rebalance date and writes nothing |

Every stage accepts `--check` (config and imports only). Exit codes: 0 ok, 1 failed, 2 busy (run lock), 3 gate not met
(the hourly retry tries again; no email until the final Sunday attempt). A target file is never edited; a rerun for the
same date is a no-op unless `--force`, which renames the old file `targets_{date}.superseded_{time}.json`.

Configuration: `app/config/analyst.json`. The `limits` block in the target file is informational: the Risk Manager ignores it and takes
its limits from `risk.json`. It ships with the strategy repository's numbers and `"placeholders": true`, which
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

Signals only needs the SMTP variables; the Ledger and Probe also need the four `ANGEL_*` ones. A value still written as
`<set at deployment>` counts as unset. Task Scheduler triggers: Signals Friday 21:30 repeated every 60 minutes for 48.5
hours; Ledger Monday-Friday 16:30 repeated every 60 minutes for 4 hours; neither starts a new instance while one runs.

**Ledger go-live checklist:** create a SmartAPI key, fill the env-file, run the Probe and fix `broker.EXPECTED` / the
parsers if any field differs (and set `ledger.observedQtyFields` per what `t1quantity` means), fill
`app/config/seed_positions.csv` from your contract notes (`ticker,qty,entry_date,entry_price`), then schedule the Ledger.
The tradebook only holds the current day, so a day the Ledger misses is recovered by reconciliation as ESTIMATED
journal rows that you correct (edit the entry/exit fields; the next run recalculates and marks the row MANUAL_VERIFIED).
The backups sit on the same disk as the data: also back up the host folder elsewhere.

### Risk Manager (daily and weekly signals)

Design: `doc/Risk_Manager_TDD.md` (read its "As-built decisions" first). It turns the Analyst's weekly target list and the
Ledger's book into `app/data/risk/signals/signals_{asOf}.json`: what to buy or sell, how many shares and why. It keeps the account
inside the limits in `risk.json` and measures how the strategy is doing. **Signals only**: it places no orders, has no order,
GTT or broker call and holds no Angel One secret. Four blocks share the code: Sizer (weekly quantities, risk-based and capped),
Risk Monitor (daily trailing stops, drawdown ladder, exposure caps, surveillance exits, tax deferral, cooldown), Signal Writer
(one file) and the reporting-only Evaluator.

| Stage | Command | When | What it does |
|---|---|---|---|
| Surveillance | `python -m app.risk.surveillance` | Mon-Fri 20:15 IST, hourly retry until 22:15 | Downloads the NSE ASM, GSM, trade-for-trade and price-band lists (sources from `risk.json`), normalises them to `surveillance/surveillance_{date}.json`. Exits 0 once today's list is complete |
| Run | `python -m app.risk.run` | Mon-Thu 21:45 IST, hourly retry until 08:00 next day; Fri 21:45 until Mon 08:00 | Gate (Ticker Data ok for asOf, `^NSEI` row, Ledger succeeded, at most 10% rows missing, targets on rebalance days), cash and NAV, trailing stops and ladder, Sizer on rebalance days, the signal file, the shadow portfolio, backups |
| Evaluate | `python -m app.risk.evaluate` | Mon-Fri 22:30 IST, hourly retry until 08:00 | Verifies the NAV rows, appends `nav/positions_daily.csv`; on Fridays writes `reports/stats_{date}.json` and `reports/tax_{FY}.json`; digest. Never changes signals or state |
| Probe | `python -m app.risk.probe --check-nse` | Manual, before go-live and after any NSE site change | Fetches the candidate NSE list pages and prints file locations and column names (never cookies) |

Every stage accepts `--check` (config and imports only, no network, no writes). Exit codes: 0 ok, 1 failed, 2 busy (run lock),
3 gate not met (the hourly retry tries again; no email until the final attempt, which becomes a failed run "no signals for {asOf}").
A signal file is written once; a rerun for the same asOf is a no-op unless `--force`, which renames the old file
`signals_{asOf}.superseded_{time}.json`. Nothing is ever liquidated because of a technical fault.

What the rules do (all numbers in `app/config/risk.json`, no code change needed):

- **Stops**: close-based trailing stop per position, highest AdjClose since entry minus 3.5 x ATR20, clamped per bucket (Large 10-18%,
  Mid 14-22%, Small 18-28%), ratcheting, replayed from the entry date every run; a breach on a skipped day is a STOP with `lateBreach`.
  A STOP repeats every run until the book no longer holds the ticker, then a 10-trading-day cooldown applies to re-entry.
- **Ladder**: drawdown of the time-weighted NAV index steps exposure down at once (-10/-15/-20/-25% to 75/50/25/0% invested) and back
  up one rung per Friday only in BULL or TREND with the index above its 20-day low. Rung 4 is flat and stays flat until you set
  `ladder.restartFrom` to a date; the restart resets the peak and starts at rung 3.
- **Sizing**: 1.25% of NAV risked to the stop, per-name caps (Large 10%, Mid 8%, Small 6%), bucket budget = composition weight x NAV x
  exposure cap, minimum order Rs 25,000, ADV participation cap, cash buffer 2%, portfolio heat cap 12%; unused budget stays cash.
  Held names are resized only outside a no-trade band and above Rs 10,000.
- **Tax deferral**: a rank-based DROP within 28 days of the 12-month mark, with at least 10% unrealised gain and the close above the
  trend MA, is held (`HOLD_DEFERRED`) and released on the anniversary; any stop, ladder, surveillance or trend exit overrides it.
- **Surveillance**: any ASM, GSM, trade-for-trade or band <= 5% flag blocks new buys; GSM and trade-for-trade also force an exit.
  A missing or stale list blocks buys (`NO_SURVEILLANCE_DATA`) but never delays exits.
- **Shadow portfolio**: follows every signal exactly (next raw Open, the Analyst's slippage and charges) from your NAV at go-live, with
  its own cooldown and ladder, so the Friday report can show the tracking gap and `signalFollowedRate`.

Configuration: `app/config/risk.json` (every tunable, validated at load) and `app/config/cash_flows.csv`
(`date,type,amount_inr,note`; one `OPENING` row with the cash balance of the tracked universe, then deposits, withdrawals, dividends;
you edit it, the Risk Manager never writes it). `app/config/nse_calendar.json`, `analyst.json` (cost model, read-only),
`config.json` and `indices.json` are shared. Reads `app/data/market/`, `app/data/storage/` and `app/data/analyst/` read-only and writes only
`app/data/risk/` (`signals/`, `state/`, `nav/`, `shadow/`, `reports/`, `surveillance/`, `backup/`, `risk_status.json`, `.lock`).
Read `risk_status.json` first if you consume the signals; a `.lock` file means a run is in progress; a file whose `executionDate` has
passed is history, not an instruction. Only the SMTP variables are needed (same env-file as the Analyst):

```powershell
docker run --rm `
  -v C:\ProgramData\ticker-pipeline\data:/app/app/data `
  -v C:\ProgramData\ticker-pipeline\config:/app/app/config `
  --env-file C:\ProgramData\ticker-pipeline\secrets\analyst.env `
  ghcr.io/debarpan-bose-chowdhury/swing-trading-risk:latest python -m app.risk.run
```

Task Scheduler triggers: Surveillance Mon-Fri 20:15 repeated every 60 minutes for 2 hours; Run Mon-Thu 21:45 repeated every 60
minutes for 10.25 hours and Friday 21:45 for 58.25 hours; Evaluate Mon-Fri 22:30 repeated every 60 minutes until 08:00; none starts a
new instance while one runs. Daily order: Ledger 16:30, Metadata 19:30, Surveillance 20:15, Ticker Data 21:00, (Fridays) Analyst
Signals 21:30, Run 21:45, Evaluate 22:30. Run's gate watches Ticker Data's status file, so an Updator overrun only delays it.

**Risk Manager go-live checklist:** fill `cash_flows.csv` with the OPENING row (and any later flows); calibrate `risk.json` (ATR%
distribution per bucket, heat cap, name caps and minimum order against your capital); run `python -m app.risk.probe --check-nse`, then
fill `surveillance.sources` (`url`, `format`, `symbolColumn`, `valueColumn`, optional `termColumn`, `filterColumn` / `filterValues`,
`rowsKey`) until Surveillance accepts it; set `evaluator.riskFreeRatePct`, the tax rates (confirm with your CA), the SMTP values and
the holidays in `nse_calendar.json`; finish the Ledger go-live checklist; back up the host folder (the 30-day backups share its disk).
Until the sources are filled Surveillance exits 1 and Run blocks every buy as `NO_SURVEILLANCE_DATA`; exits keep working.

### Backtest (personal use, sibling `backtest/`)

Replays the Risk Manager's own `decide()` over NSE history with next-open fills, dividends, a FIFO tax overlay, walk-forward
windows with a locked holdout, and an overfitting gate. It reads `app/` and `app/data/` and writes only `backtest/data/`
(git-ignored); it never places orders and is not copied into any image. Design: `doc/Backtest_Engine_TDD.md`; decision
history and measured results: `doc/Backtest_Implementation_Plan.md`.

```
uv run --project backtest python -m backtest.run --check          # config + app side, no network, no writes
uv run --project backtest python -m backtest.prep --check         # is the data ready
uv run --project backtest python -m backtest.prep --dividends     # Yahoo dividends and splits (network)
uv run --project backtest python -m backtest.run --single --set sizing.minNewOrderInr=3000 --set sizing.minAdjustmentInr=1500   # one judge run (Rs 1 lakh needs lower sizing minimums to trade) -> backtest/data/runs/run_*.json
uv run --project backtest python -m backtest.run --compare --set sizing.minNewOrderInr=3000 --set sizing.minAdjustmentInr=1500   # today's names vs point-in-time, 0/50/100% write-off of vanished names
uv run --project backtest pytest -c backtest/pyproject.toml       # offline tests
```

Point-in-time universe (needs a machine that can reach NSE): `backtest.bhav --probe`, `--download`, `--build`, then
`backtest.universe --links`, `--validate-adjust`, `--tune-adjust`, `--build-pit`; set `universe.mode` to `pit` once
`universe.adjustValidated` is true. Before relying on a result, confirm `backtest/config/params.json` (`confirmed`) and the tax
table (`tax.confirmed`). Results are labelled in every report (surveillance not modelled, current-rate charges, universe bias).

## Container

`Dockerfile` builds the HTTPS service image; `Dockerfile.market` builds the Ticker Data System image (adds
`yfinance`, `pandas`, `pyarrow`; default command `python -m app.market.updator`); `Dockerfile.analyst` builds the Stock Analyst
image (default command `python -m app.analyst.signals`); `Dockerfile.risk` builds the Risk Manager image (default command
`python -m app.risk.run`, no broker secrets). The service image starts the HTTPS service. Mount a directory containing `cert.pem` and
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
- `app/analyst/`: Stock Analyst (`ledger`, `journal`, `broker`, `probe`, `signals`, `regime`, `selector`, `costs`, `secrets`, shared `common`).
- `app/risk/`: Risk Manager (`surveillance`, `run`, `evaluate`, `probe`; `sizer`, `monitor`, `stops`, `ladder`, `nav`, `shadow`, `evaluator`, `tax`, `surveil`, shared `common`).
- `app/config/`: `config.json` (metadata pipeline), `market.json`, `indices.json`, `nse_calendar.json` (market stages), `analyst.json`, `seed_positions.csv` (analyst), `risk.json`, `cash_flows.csv` (risk).
- `app/data/`: pipeline output (`raw/`, `storage/`, `market/`, `logs/`, `health.json`); git-ignored.
- `backtest/`: backtest engine (sibling project with its own `pyproject.toml`; config in `backtest/config/`, output in `backtest/data/`, git-ignored).
- `tests/app/`: unit tests for the HTTP service, the metadata pipeline, the market stages, the Analyst and the Risk Manager (Yahoo, NSE and the broker are always faked).
- `tests/backtest/`: offline tests for the sibling `backtest/` package (run with its own project, see CLAUDE.md).

## CI/CD

`.github/workflows/ci-cd.yml` only calls the reusable workflows in
[`debarpan-bose-chowdhury/CI-CD`](https://github.com/debarpan-bose-chowdhury/CI-CD) (CI + CodeQL + SBOM, multi-image
Docker build/push to GHCR, staging deploy, smoke + OWASP ZAP DAST, production deploy). Same-repo pull requests also build,
stage and scan (without moving `:latest`); only pushes to `main` deploy to production. `build.yml` runs the tests and
the SonarQube scan. Required secrets: `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`, `SONAR_TOKEN`; configure the
`staging` and `production` GitHub environments before enabling deployments. All pipeline logic is shared and lives in
CI-CD; this repo only supplies the inputs (the `images` list, compose files, HTTPS health URL, `tls-cert: true`).

The images are built in one `build-docker` job (an `images` list passed to the shared workflow):

| Image | Dockerfile | Role | Pipeline |
|---|---|---|---|
| `swing-trading-system` | `Dockerfile` | `service` | build, staging deploy, DAST, production deploy |
| `swing-trading-market` | `Dockerfile.market` | `batch` | build, smoke-run, weekly scan (runs from Windows Task Scheduler, never deployed) |
| `swing-trading-analyst` | `Dockerfile.analyst` | `batch` | same as the market image (smoke-run `python -m app.analyst.signals --check`) |
| `swing-trading-risk` | `Dockerfile.risk` | `batch` | same as the market image (smoke-run `python -m app.risk.run --check`) |

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
