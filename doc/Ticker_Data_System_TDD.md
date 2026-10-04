# Ticker Data System — Technical Design Document

Sep 29, 2026 · Author: @Deba · Status: Ready for implementation review

## 1. Overview

A daily batch pipeline that maintains OHLCV price history for the NSE tickers selected by the upstream **NSE Ticker Metadata Pipeline**, plus a configurable list of index series. Data comes from `yfinance`, is validated row by row, and is stored in two tiers: recent data as per-ticker **CSV** (fresh tier) and data older than one year as per-ticker **Parquet + zstd** (archive tier).

It runs on the same single Windows machine as the upstream pipeline, as separate Docker containers started by Windows Task Scheduler. There is no cloud infrastructure, message queue, or database.

**Stages**

| Stage | Trigger | Purpose |
|---|---|---|
| Data Validator | Called by every stage (shared module) | Row-level checks; rejects bad rows, requests refetch of null/throttled values |
| Migrator | Manual, once in the system's lifetime | Bootstrap: seed registry, download max history, split into fresh/archive |
| Updator | Automatic, daily 21:00 IST | Refresh registry, gap-fill and append new days, detect corporate actions |
| Archiver | Automatic, quarterly (2nd of Jan/Apr/Jul/Oct, 22:00 IST) | Move rows older than 1 year from fresh CSV to archive Parquet |
| Historical Data | Storage (not a process) | The fresh and archive tiers and their rules |

**Out of scope:** the upstream pipeline itself (see its own TDD), and the downstream consumers of this data. Only the read contract (§9) is defined here.

## 2. Decision Log

Every conflict or gap found in the original *Ticker Data* document was raised with you. The table records what was decided. "Proposed" means the redesign offered in the review was chosen over the original.

| # | Topic | Original design | Decision |
|---|---|---|---|
| D1 | Archive cutoff | Migrator: 2 years; Archiver/Historical Data: 1 year (conflict) | **1 year everywhere** (Proposed) |
| D2 | Updator trigger | "Daily when healthy" vs "manual" (conflict) | **Automatic daily** (Proposed) |
| D3 | Ticker universe | Not defined | **Append-only registry** of every symbol ever seen upstream (Proposed) |
| D4 | Storage layout | `{First}_{Last}.csv`, no ticker in name, multi-ticker implied | **Per-ticker files**: `fresh/{Ticker}.csv`, `archive/{Ticker}/Compressed_{First}_{Last}.parquet` (Proposed) |
| D5 | Price adjustment | Not stated | **Raw OHLCV + AdjClose** (`auto_adjust=False`) (Proposed) |
| D6 | Failure alerts | "Fire an email alert" per failure | **One digest email per run** plus a reject file (Proposed) |
| D7 | Trading-day calendar | "NSE/BSE calendar", no source | **Maintained `nse_calendar.json`** in config (Proposed) |
| D8 | Deployment | Not stated | **Same host, same Docker + Task Scheduler pattern**, data under `app/data/market/` (Proposed) |
| D9 | New tickers vs "Migrator runs once" | Conflict | **Updator backfills new tickers**; Migrator is gated by a `migration.done` marker with per-ticker checkpoint (Proposed) |
| D10 | Concurrency | "One process per file at a time", no mechanism | **Global run lock + atomic replace** (Proposed) |
| D11 | Rate limiting | Migrator 50/15 s; Updator "as fast as possible" | **Shared throttle for all fetchers** (Proposed) |
| D12 | Retries | "Retry on failure", unspecified | **Bounded retries, then defer to next day** (Proposed) |
| D13 | Row schema | Column names/types/ticker format not stated | **Locked schema** (§5.2) with NSE symbol as `Ticker` (Proposed) |
| D14 | Index data | "Maintain a list of index" | **Config list, separate folder**; starting list `^NSEI`, `^NSEBANK`, `^BSESN` (Proposed) |
| D15 | Archiver safety | Compress, append, delete; no backup (accepted risk) | **Verify-then-delete, immutable archive**; no-backup risk stays accepted and flagged (Proposed) |
| D16 | Delisted / no-data tickers | Not handled | **Mark inactive after 5 consecutive no-data trading days** (Proposed) |
| D17 | Gap detection | "Last Date to Current Date" | **Per-ticker, calendar-aware, plus 30-day lookback scan** (Proposed) |
| D18 | Schedule | Not stated | **Updator 21:00 IST daily; Archiver 22:00 IST on the 2nd of Jan/Apr/Jul/Oct** (Proposed) |
| D19 | Consumer contract | "Reads span partitions natively" | **`status.json` + direct file reads** (Proposed) |
| D20 | Packaging / config | Not stated | **Separate Docker image, own config `market.json`** (Proposed) |
| D21 | Health gate | Whole Updator waits for `health.json` | **Gate only the registry refresh**; prices for known tickers always update (Proposed) |
| D22 | Email transport | Not stated | **SMTP, credentials from environment variables** (Proposed) |
| D23 | Tunable numbers | Various | **Accepted as config defaults** (§7) |
| D24 | Splits / dividends | Not handled | **Detect the event, then rebuild that ticker's full history** (Proposed) |
| D25 | Duplicates vs upsert | "Reject duplicates" and "upsert" (conflict) | **Duplicates inside an incoming batch are rejected; keys already stored are upserted** (Proposed) |
| D26 | Rejected rows vs per-ticker atomicity | Conflict | **Commit valid rows, quarantine bad ones; roll back a ticker only on error or high reject share** (Proposed) |

**Kept from the original, unchanged:** `yfinance` as source; CSV fresh tier and Parquet+zstd archive; composite key (Ticker, Date); daily frequency; download 50 tickers at a time with a 15 s gap; indices downloaded one at a time with `period=max` plus retry, jitter and backoff; Archiver runs every 3 months; no backup or versioning, accepted and flagged with a ⚠.

## 3. Architecture

```
            NSE Ticker Metadata Pipeline (upstream, read-only here)
            app/data/storage/*.csv     app/data/health.json
                        │                        │
                        └──────────┬─────────────┘
                                   ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  Updator  (daily 21:00 IST)                                  │
   │   1. take lock            2. refresh registry (if healthy)   │
   │   3. plan gaps            4. fetch (50/batch, 15 s, budget)  │
   │   5. Validator ──▶ commit valid rows per ticker (atomic)     │
   │   6. corporate-action rebuild   7. status.json + digest mail │
   └───────────────┬──────────────────────────────────────────────┘
                   ▼
   app/data/market/
      registry.csv   fresh/{Ticker}.csv   archive/{Ticker}/*.parquet
      indices/…      status.json          .lock
                   ▲                              ▲
   Migrator (manual, once)            Archiver (quarterly, 22:00 IST, 2nd of Jan/Apr/Jul/Oct)
   seed registry, max history,        rows older than 1 year: CSV ──▶ Parquet+zstd, verify, then delete
   split at 1 year
                   │
                   ▼
   Consumers read fresh/ + archive/ directly; check status.json first
```

All stages use the shared **Validator** module and the shared **fetch/throttle** module. The **run lock** guarantees only one stage touches `app/data/market/` at a time.

## 4. Deployment & Orchestration

| Property | Value |
|---|---|
| Language | Python |
| Host | The same Windows machine as the upstream pipeline. No cloud, no server |
| Packaging | One Docker image, separate from upstream's. Contains `migrator.py`, `updator.py`, `archiver.py` and the shared modules (validator, fetcher/throttle, store, mailer) |
| Trigger | Windows Task Scheduler, one entry per stage, each running `docker run --rm -v C:\ProgramData\ticker-pipeline\data:/app/data -v C:\ProgramData\ticker-pipeline\config:/app/config -e SMTP_USER -e SMTP_PASSWORD <image> python <stage>.py` |
| Volumes | Same bind-mounts as upstream (path inherited from the upstream TDD, which still lists it as "confirm before implementation") |
| Config | `app/config/market.json` (new file, next to upstream's `config.json`) |
| Secrets | SMTP username and password come only from environment variables, never from files |
| Upstream files | `app/data/storage/` and `app/data/health.json` are read-only for these stages. They never write to `raw/`, `storage/` or `health.json` |
| Daily order | Upstream: Data Source 19:30 → Filter → Notifier. This system: Updator at 21:00 IST |
| Quarterly order | Upstream Cleaner 20:00 on the 1st of Jan/Apr/Jul/Oct. This system: Archiver 22:00 IST on the 2nd, so the two never share an evening |
| Migrator | Run by hand once (see §6.1) |

## 5. Data Layout and Schemas

### 5.1 Folder layout

```
app/data/market/
├── registry.csv                          # ticker universe (§5.3)
├── fresh/{Ticker}.csv                    # recent rows, one file per ticker
├── archive/{Ticker}/Compressed_{First Date}_{Last Date}.parquet
├── indices/
│   ├── fresh/{IndexKey}.csv              # IndexKey = symbol without ^ (NSEI, NSEBANK, BSESN)
│   └── archive/{IndexKey}/Compressed_{First Date}_{Last Date}.parquet
├── status.json                           # last-run summary for consumers (§9)
├── migration.done                        # written once by Migrator on success
├── migration_checkpoint.json             # per-ticker progress during migration
└── .lock                                 # run lock (§7.1)

app/data/logs/
├── market_{stage}_{date}.log
└── rejects/{date}_{stage}.csv            # rejected rows with reason codes
```

Ticker names are used as file names as they are (for example `M&M.csv`, `BAJAJ-AUTO.csv`). Archive file names follow the original convention `Compressed_{First Date}_{Last Date}.parquet`.

### 5.2 Row schema (identical for CSV, Parquet, equities and indices)

| Column | Type | Rule |
|---|---|---|
| `Ticker` | string | NSE symbol without suffix (for example `RELIANCE`). For indices, the Yahoo index symbol (for example `^NSEI`) |
| `Date` | date | ISO `YYYY-MM-DD`, IST trading date, no time component |
| `Open`, `High`, `Low`, `Close` | float64 | Unadjusted for dividends (`auto_adjust=False`). Yahoo restates them after splits (see §6.2 step 6) |
| `AdjClose` | float64 | Yahoo's adjusted close |
| `Volume` | int64 | ≥ 0. Indices may be 0 |

Files are sorted by `Date` ascending. `(Ticker, Date)` is unique within stored data.

### 5.3 `registry.csv`

| Column | Meaning |
|---|---|
| `nse_symbol` | Symbol as in upstream bucket files |
| `yahoo_symbol` | `nse_symbol` + `.NS` |
| `status` | `active` or `inactive` |
| `first_seen` | Date first found in an upstream bucket file |
| `last_seen_upstream` | Latest upstream file date containing the symbol |
| `no_data_days` | Consecutive trading days Yahoo returned no data |
| `inactive_since` | Date set inactive, blank if active |

The registry is append-only: symbols are never deleted, so history survives when a ticker leaves the upstream top-N lists.

## 6. Components

### 6.1 Data Validator (shared module)

Runs on every batch from every stage. Each rule failure produces a reason code in `logs/rejects/{date}_{stage}.csv`.

| # | Rule | Reason code |
|---|---|---|
| 1 | Schema/type check on `Ticker`, `Date`, `Open`, `High`, `Low`, `Close`, `AdjClose`, `Volume` | `SCHEMA` |
| 2 | OHLC sanity: `High ≥ Open, Close, Low`; `Low ≤ Open, Close, High`; `Volume ≥ 0` | `OHLC` |
| 3 | Duplicate `(Ticker, Date)` **within an incoming batch**: every copy is rejected. Keys that already exist in stored data are not rejected; they are upserted (D25) | `DUP_IN_BATCH` |
| 4 | Trading-day check against `nse_calendar.json` (weekends, NSE holidays, listed special sessions) | `NON_TRADING_DAY` |
| 5 | Null or throttled values: not rejected immediately; the ticker/date goes back on the fetch list and is refetched and replaced (at most 2 refetch passes per run). If still null after that, the row is rejected | `NULL_VALUE` |
| 6 | On any rejection: log the row, include it in the run's digest email | — |

A "throttled value" means a null/NaN field in a returned row, or a row missing because the request hit a rate limit (HTTP 429/999, `YFRateLimitError`).

**Commit rule (D26).** After validation, the valid rows of a ticker are committed atomically for that ticker. Rejected rows are quarantined, and their dates are picked up by the next run's gap-fill and lookback scan. A ticker is instead rolled back completely (nothing written) when the fetch or write raises an error, or when more than **20%** of its returned rows are rejected, which points to a bad response.

### 6.2 Migrator

Manual, run once in the system's lifetime, after upstream has produced at least one healthy set of bucket files.

1. If `migration.done` exists, exit.
2. Take the run lock. Seed `registry.csv` from every bucket file currently in `app/data/storage/` (requires `health.json` healthy and today's `checkedAt`; otherwise exit, since there is nothing to seed from).
3. For every registry ticker, download **max history** with `yfinance`, 50 tickers per batch, 15 s between the end of one call and the start of the next, under the shared throttle (§7.2).
4. For each index in `indices.json`: one index at a time, `period=max`, daily timeframe, with retry, jitter and backoff on transient errors.
5. Downloaded data goes through the **Validator** first, then to storage. This keeps a bad response from corrupting the bootstrap and lets the migration continue.
6. Split at the archive cutoff (§6.4): rows older than 1 year → one Parquet+zstd partition; the rest → the fresh CSV.
7. **Per ticker, atomic:** archive and fresh files for a ticker are written to a temporary folder and swapped in together (the shared `rebuild_ticker` routine, also used for corporate actions). Upsert on `(Ticker, Date)`. After each ticker commits, it is recorded in `migration_checkpoint.json`. A crashed run resumes from the checkpoint; a half-written ticker is simply redone.
8. When every registry ticker and index has committed, write `migration.done` and release the lock.
9. Retries follow the shared retry contract (§7.3).

### 6.3 Updator

Automatic, daily at 21:00 IST.

1. Take the run lock. If it is busy, exit with a logged "busy" and a non-zero code.
2. **Registry refresh (gated by upstream health, D21).** If `app/data/health.json` reports `status: healthy` and its `checkedAt` date is today (IST), read every bucket file in `app/data/storage/`, append any new symbol to `registry.csv`, update `last_seen_upstream`, and re-activate an inactive ticker that has reappeared. Otherwise skip the refresh, log why, and say "registry not refreshed" in the digest. Price updates continue either way.
3. **Plan gaps (D17).** For each `active` ticker and each index:
   - *End date:* the most recent NSE trading day whose session is final, where a session is treated as final after `sessionFinalAfterIST` (default 20:00). No partial bars are ever stored.
   - *Start date:* the ticker's last stored date + 1. A ticker with no stored history is fully backfilled (`period=max`) through the same code path as Migrator.
   - *Lookback scan:* also list any trading day in the last **30 calendar days** with no stored row, and add it to the fetch.
4. **Fetch** in batches of 50 with a 15 s gap, under the shared throttle and retry contract (§7.2, §7.3). Each fetch also requests dividend and split events.
5. **Validate** (§6.1) and **commit** valid rows per ticker: read fresh CSV, upsert on `(Ticker, Date)`, write to a temp file, atomic rename.
6. **Corporate actions (D24).** If a split or dividend event falls in the fetched window, or the rows in the lookback overlap disagree with stored values, that ticker is fully re-fetched (max history), validated, and rebuilt through `rebuild_ticker`: fresh CSV and archive Parquet partitions are replaced together. This is the only permitted change to archive files. Events are listed in the digest.
7. **No-data tracking (D16).** A ticker for which Yahoo returns an empty response on a trading day (not a rate-limit block) has `no_data_days` incremented; any successful row resets it to 0. At **5** consecutive days the ticker is set `inactive` (no more fetches, history kept) and listed in the digest.
8. Write `status.json`, send the digest email, release the lock.

### 6.4 Archiver

Automatic, quarterly. Config cron: `0 22 2 1,4,7,10 *` (22:00 IST on the 2nd of Jan/Apr/Jul/Oct).

Archive cutoff = run date − **365 days**. For each ticker and index, under the run lock:

1. Read the fresh CSV. Rows with `Date` earlier than the cutoff are the "aged" set. If none, skip. If the file straddles the cutoff, only the aged rows move; the rest stay.
2. Write the aged rows to a **temporary** Parquet file (zstd, tuned for load time and compression ratio).
3. **Verify:** re-read the Parquet and check that the row count and the set of `(Ticker, Date)` keys match the aged rows exactly.
4. Atomically rename the temp file to `archive/{Ticker}/Compressed_{First Date}_{Last Date}.parquet`.
5. Only then rewrite the fresh CSV without the aged rows (temp file + atomic rename).

A crash between steps 4 and 5 leaves duplicates (rows in both tiers), never a loss. On the next run, rows already present in an archive partition are simply dropped from the CSV. Archive files are never edited, except by the corporate-action rebuild in §6.3.

### 6.5 Historical Data (storage rules)

1. Data older than 1 year lives in the archive tier as Parquet+zstd. Data up to 1 year old lives in the fresh tier as uncompressed CSV.
2. Compression runs every 3 months (Archiver). Between runs the fresh CSV can hold up to about 3 extra months of rows older than 1 year; consumers must not assume the fresh tier is trimmed.
3. Archive partitions are appended per ticker; reads span the partitions natively (for example DuckDB or pandas over `archive/{Ticker}/*.parquet`), with no manual decompress-and-merge step.
4. Naming: archive `Compressed_{First Date}_{Last Date}.parquet` (inside the ticker's folder); fresh `{Ticker}.csv`.
5. After compressing a batch, the archived rows are removed from the CSV (§6.4). A batch straddling the cutoff is split first.
6. **⚠ No backup or versioning.** Accepted risk, carried over from the original design.
7. A file is updated by only one process at a time, enforced by the run lock and atomic replace (§7.1).

## 7. Cross-Cutting Behaviour

### 7.1 Concurrency

Every stage takes `app/data/market/.lock` at start with an atomic create; the file holds the PID and a timestamp. A stage that cannot get the lock exits with a logged "busy" and a non-zero code. A lock older than **6 hours** is treated as stale and may be taken over. Every file write goes to a temp file and is then atomically renamed, so readers never see a half-written file.

### 7.2 Throttling (shared by Migrator and Updator)

| Setting | Default |
|---|---|
| Batch size | 50 tickers |
| Gap between batches | 15 s (from end of one call to start of the next) |
| Hourly request budget | 2,000 (each ticker attempt counts as one request) |
| On 429 / 999 / `YFRateLimitError` | Stop fetching, save progress, sleep 60 minutes, resume the remaining tickers |
| Max block-pauses per run | 3. Anything still unfetched is left for the next day's gap-fill |

### 7.3 Retries

Per request: **3 retries** with **2 / 4 / 8 s** exponential backoff plus jitter (same shape as upstream). Null/throttled rows get at most **2 refetch passes** per run. A ticker that still fails is not written, is listed in the digest, and is picked up automatically by the next run's gap-fill.

### 7.4 Health gate

Only the registry refresh depends on upstream's `health.json` (§6.3 step 2). Price updates for known tickers always run, so an upstream outage does not make price data go stale.

### 7.5 Alerts and logging

- Every stage logs to `app/data/logs/market_{stage}_{date}.log` and to stdout.
- Rejected rows go to `app/data/logs/rejects/{date}_{stage}.csv` with reason codes.
- **One digest email per run**: counts of rejections per rule and the top offending tickers, tickers that failed after retries, tickers set inactive, split/dividend rebuilds, "registry not refreshed" if applicable, and any fatal error. A failed email send is logged and never fails the run.
- SMTP: host, port 587 with STARTTLS, sender and recipient list in `market.json`; username and password only from environment variables.

## 8. Configuration — `app/config/market.json`

Schema with the accepted defaults (D23):

```json
{
  "paths": {
    "market": "app/data/market",
    "upstreamStorage": "app/data/storage",
    "upstreamHealth": "app/data/health.json",
    "logs": "app/data/logs",
    "calendar": "app/config/nse_calendar.json",
    "indices": "app/config/indices.json"
  },
  "fetch": {
    "batchSize": 50,
    "batchGapSeconds": 15,
    "hourlyRequestBudget": 2000,
    "blockPauseMinutes": 60,
    "maxBlockPausesPerRun": 3,
    "maxRetries": 3,
    "backoffSeconds": [2, 4, 8],
    "nullRefetchPasses": 2,
    "sessionFinalAfterIST": "20:00"
  },
  "validator": {
    "rejectShareRollback": 0.20
  },
  "updator": {
    "lookbackDays": 30,
    "deadTickerNoDataDays": 5,
    "dailyRunTime": "21:00"
  },
  "archiver": {
    "cutoffDays": 365,
    "cron": "0 22 2 1,4,7,10 *",
    "parquet": { "compression": "zstd" }
  },
  "lock": { "staleAfterHours": 6 },
  "mail": {
    "smtpHost": "<set at deployment>",
    "smtpPort": 587,
    "sender": "<set at deployment>",
    "recipients": ["<set at deployment>"]
  }
}
```

`config/indices.json` — starting list:

```json
{ "indices": ["^NSEI", "^NSEBANK", "^BSESN"] }
```

`config/nse_calendar.json` — updated once a year from NSE's official holiday circular; lists holidays and special sessions (for example Muhurat trading).

## 9. Consumer Contract

After every run, the stage writes `app/data/market/status.json`:

```json
{
  "status": "ok | partial | failed",
  "stage": "updator",
  "runDate": "2026-09-29",
  "finishedAt": "2026-09-29T21:14:03+05:30",
  "lastTradingDay": "2026-09-29",
  "tickersUpdated": 148,
  "tickersFailed": ["XYZ"],
  "tickersInactive": [],
  "rowsRejected": 3,
  "registryRefreshed": true
}
```

Consumers read `fresh/` and `archive/` directly (for example pandas or DuckDB over the Parquet folder) and must check `status.json` first. If the `.lock` file exists, a run is in progress. Consumer implementations are out of scope.

## 10. Failure Modes

| Failure | Behaviour |
|---|---|
| Yahoo rate-limit block (429/999) | Save progress, sleep 60 min, resume; max 3 pauses; remainder deferred to next day |
| Ticker fetch fails after retries | Not written; listed in digest; retried by next run's gap-fill |
| Bad row(s) | Row quarantined to reject file; other valid rows commit; dates refetched next run |
| More than 20% of a ticker's rows rejected | Ticker rolled back; listed in digest |
| Upstream unhealthy or stale `health.json` | Registry not refreshed; prices still updated; noted in digest |
| Ticker returns no data 5 trading days in a row | Set `inactive`; history kept; listed in digest |
| Split or dividend detected | Full re-fetch and rebuild of that ticker; listed in digest |
| Crash mid-Migrator | Resume from `migration_checkpoint.json`; unfinished ticker redone |
| Crash between Parquet write and CSV rewrite (Archiver) | Duplicates across tiers, no loss; cleaned on next run |
| Lock held by another stage | Exit "busy", non-zero code; a lock older than 6 h is taken over |
| Email send fails | Logged; run result unaffected |
| Disk loss | No recovery. No backup by design (⚠ accepted risk) |

## 11. Known Limitations and Risks

- **No backup or versioning** (accepted, carried over from the original design).
- **Single data source.** Everything depends on `yfinance`/Yahoo behaviour, availability and rate limits.
- **Fresh tier is not strictly ≤ 1 year.** It can hold up to about 3 additional months of older rows until the next Archiver run.
- **Sensex (`^BSESN`) is a BSE index** but is validated against the NSE calendar. A day when BSE was open and NSE closed would have its row rejected as `NON_TRADING_DAY`.
- **Adjusted values depend on Yahoo.** `AdjClose` is only as fresh as the last rebuild for that ticker.
- **Request accounting is conservative.** Each ticker attempt counts as one request against the hourly budget.

## 12. Inputs Needed at Deployment (not design open items)

- SMTP host, sender, recipient list, and the `SMTP_USER` / `SMTP_PASSWORD` environment variables.
- Confirm the Yahoo index symbols `^NSEI`, `^NSEBANK`, `^BSESN` resolve on the day of first run.
- The first `nse_calendar.json`, and a yearly reminder to refresh it.
- Confirm the bind-mount path `C:\ProgramData\ticker-pipeline` (inherited from the upstream TDD).
- Two details were fixed while writing rather than asked: `sessionFinalAfterIST` (default 20:00, so the 21:00 run only stores final bars) and the request-accounting rule (one ticker attempt = one request). Change them in `market.json` if you disagree. Everything else in this document was confirmed by you.
