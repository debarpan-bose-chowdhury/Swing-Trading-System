# Stock Analyst — Technical Design Document

Oct 1, 2026 · @Deba · Status: Signals implemented, Ledger and Probe pending (see "As-built decisions")

The Stock Analyst is two batch jobs on the trading host: an end-of-day ledger that mirrors your Angel One portfolio into a trading journal, and a Friday signal run that turns the Nifty 50 regime and per-bucket momentum into a weekly target list for the Risk Manager. It places no orders.

## Overview

The Analyst reads three sources and writes two outputs. It reads your Angel One portfolio (holdings, positions, tradebook), the Ticker Data System's price history and the Metadata pipeline's bucket files. It writes a trading journal every trading day and a target list every Friday. Order placement, position sizing and drawdown enforcement belong to the Risk Manager and execution components, which are out of scope.

| Stage | Command | Trigger | Purpose |
|---|---|---|---|
| Ledger | python -m app.analyst.ledger | Task Scheduler, Mon to Fri 16:30 IST, hourly retry until 20:30 | Log in to Angel One, snapshot the portfolio, record fills, update the open-position book, write journal rows for closed positions, purge old snapshots, back up the ledger files |
| Signals | python -m app.analyst.signals | Friday 21:30 IST, hourly retry until Sunday 22:00 | Check Ticker Data is ready, compute the regime, select stocks per bucket, write the target list with KEEP, ADD and DROP against current holdings |
| Probe | python -m app.analyst.probe --check-broker | Manual, before go-live and after any Angel One change | Log in and list the field names Angel One returns, so the parsers can be confirmed |

Every stage accepts --check (load config, prove imports, print <stage>: check ok, no network, no writes), matching the other stages in the repo.

**Out of scope:** the Risk Manager, order placement and any order-intent file, backtesting, and the Metadata and Ticker Data systems themselves (only their read contracts are used here).

**Sources of this design:** your Stock Analyst notes, the Metadata TDD, the Ticker Data TDD and the repo README, plus an Angel One SmartAPI research pass dated 30 Sep 2026. The official Angel One docs pages refuse automated fetches ("not authorized"), so their facts come from the official SDK source, the official forum and Angel One's own notices. Anything not confirmed is marked UNVERIFIED.

## Decision log

Every point where the original notes were unclear, conflicting or unsafe was put to you. "Proposed" means the redesign offered was chosen over the original. "Kept" means the original stands.

| # | Topic | Original design | Decision |
|---|---|---|---|
| D1 | Scope | Config, positions, journal, Artifact Handler, two classifiers, selector; Risk Manager out | Sync plus signals, stopping at the target list. No order placement, no order-intent file (Proposed) |
| D2 | Broker sync | Fetch positions, diff against Old, delete Old, fill the journal by hand | End-of-day ledger built from tradebook fills plus holdings and positions snapshots; nothing is overwritten (Proposed) |
| D3 | Missed day | Not handled | Diff against the last good snapshot, write ESTIMATED journal rows, list them in the digest for you to correct (Proposed) |
| D4 | Storage growth | "Delete the Old Position" | Daily raw snapshot files purged after 90 days; fills, book and journal are never purged; 30-day rotating backup of those three files (your choice) |
| D5 | Run gate | Runs only when Metadata health.json is healthy | Signals gated on Ticker Data status.json plus the index row for the rebalance date; Ledger gated only on broker login (Proposed) |
| D6 | Rebalance day | "Last working day in the week", no calendar source | History from index data; live run from nse_calendar.json; refuses to run if the year's holidays list is empty (Proposed) |
| D7 | Price basis | close_df and market, price type not stated | AdjClose for stocks, Close for the index (Proposed) |
| D8 | Buckets | One strategies[regime] dict; Portfolio Composition % not connected to selection | strategies[regime][bucket], selector runs per bucket, composition % passed through (Proposed) |
| D9 | Regime index | "The chosen index" | regime.index in config, ^NSEI now (Kept, made explicit) |
| D10 | Config | Eight constituents, missing the parameters the code needs | app/config/analyst.json holding all of them (Proposed) |
| D11 | Output | Selector returns top-N tickers | Target file with regime state and KEEP / ADD / DROP against holdings, no sizing (Proposed) |
| D12 | Execution cost | "Analyze and fill in" | Config table of Angel One delivery charges plus slippage bps per bucket (Proposed) |
| D13 | Parameter values | None given | Schema plus flagged placeholders; Signals refuses to run while placeholders is true (Proposed) |
| D14 | Holdings scope | Whole account implied | Only holdings in the Ticker Data registry and not in ignoreSymbols; others reported as untracked (Proposed) |
| D15 | Journal | Handler writes it, you add details by hand | trading_journal.csv, append-only, separate auto and manual columns (Proposed) |
| D16 | Login | "Login to Angel One" | Automated TOTP login from environment variables, no token written to disk (Proposed) |
| D17 | Drawdown limits | 17% per position, 50% portfolio | Kept as written, validated and passed through, not enforced here (Kept) |
| D18 | Defects in the selector code | See below | Fix all three (Proposed) |
| D19 | Regime state | Not stated | Stateless full recompute on every run (Proposed) |
| D20 | Deployment | Not stated | Separate image, Task Scheduler, run lock, digest email, status file, same pattern as Ticker Data (Proposed) |
| D21 | Invested Capital | "Fetched from broker" | Cost basis of tracked holdings (Proposed) |
| D22 | Lots and seed | Not stated | Average-cost matching; one-off seed_positions.csv for holdings that predate go-live (Proposed) |
| D23 | Symbol mapping | Not stated | Strip the series suffix and match the registry's nse_symbol (Proposed) |
| D24 | Broker access | Not stated | No SmartAPI key yet: create one and run the probe before scheduling anything |

**D18 in detail.** (a) select_rebalance_stocks calls select_stocks without the precomputed features argument and never calls prepare_stock_selection_features; the TDD computes features once per bucket and passes them. (b) A ticker with no row on the rebalance date must not be selected. The original only misbehaves here if close_df is forward-filled, and it leaves close_df construction open, so this TDD forbids forward-fill and adds an explicit check. (c) Tickers the registry marks inactive are excluded.

**Kept from the original, unchanged:** the regime formulas (SMA50, SMA200, ROC63, the 209-row Unknown rule), the persistence logic with immediate BEAR activation, the momentum plus trend-MA selection rule, weekly cadence with Monday-open execution, Angel One as broker, and the Risk Manager staying out of scope.

## As-built decisions (post-review additions D25 onward)

Raised while implementing against the built Ticker Data and Metadata systems and the original strategy repository (`DEBARPAN2000/Swing-Trading-Strategy`). Where a row conflicts with the text elsewhere in this document, this table wins. Rows marked *phase 2* describe the Ledger, which is not built yet.

| # | Topic | Decision |
|---|---|---|
| D25 | Signals gate | Checks `market/status.json` `status` (ok or partial) and `lastTradingDay`, plus the absence of `market/.lock`. It does **not** check `stage`, because every Ticker Data stage overwrites that file (an Archiver run on a Friday the 2nd would otherwise block the weekend retries; the next daily Updator run rewrites it) |
| D26 | Rebalance date | Last **Monday to Friday** trading day of the ISO week, in history and live. Weekend special sessions (Muhurat) are ignored. A week with no such day is skipped |
| D27 | `--as-of` | Read-only replay: recomputes regime and selection for the rebalance date on or before the given date, prints the target JSON, writes nothing, skips the freshness gates and the delta. It uses the bucket file on disk (the Cleaner keeps one per quarter), so it reproduces a run but is not a backtest |
| D28 | Ledger state *(phase 2)* | `ledger/fills.csv` is the single source of truth; `book.csv` is replayed from the seed plus fills (and synthetic adjustment rows for CORP_ACTION, MISSED_BUY and ESTIMATED sells). Journal rows are appended idempotently by deterministic `trade_id`, so the pending-file merge is dropped; a journal that Excel holds open catches up on the next run. The Ledger block of `analyst_status.json` keeps a persisted `lastGoodRunDate` (used for the stale-holdings check and the "already succeeded today" exit). The Ledger skips the broker login on non-trading days |
| D29 | Parameter values | Strategy numbers are the strategy repository's `REGIME_STRATEGIES` (BULL top_n 2 / lookback 126, TREND 4 / 168, WEAK 6 / 84, BEAR 8 / 63, trend MA 150, same for every bucket), persistence 4 weeks, composition LargeCap 0 / MidCap 0 / SmallCap 1, limits 17% / 50%. That repository's own guide lists different top_n values, so `placeholders` stays `true` until the numbers are confirmed |
| D30 | Selector | Trend MA needs the **full** N-row window (replaces the `min_periods=1` limitation; otherwise `insufficientHistory`). Optional `selector.momentumSkipDays` (default 0 = original formula). Liquidity filter inside the selector, all regimes: median of the last 20 days of raw `Close x Volume` must be at least `minAdvCr` (10 crore), full window required, else `illiquid`. A bucket whose composition weight is 0 gets no selection and its held tickers are DROP `NO_ALLOCATION` |
| D31 | BEAR ranking | In the BEAR regime only, stage 1 keeps every stock with momentum > 0 and price above its trend MA (no cut to top_n); stage 2 ranks them by the repository's composite score (0.40 mom20 + 0.35 mom63 + 0.20 hit20 - 0.35 vol20 + 0.10 dd63, weights in `selector.bearScore`), momentum-confirmed names (mom20 > 0 and mom63 > 0) first and the rest as backup fill, and keeps top_n. A candidate needs 70 full rows to be scored. Stage 2 never picks a stock below its trend MA |
| D32 | Regime details | Fewer than 210 index rows gives `Unknown` (the repository returned BEAR). `pending_regime` always equals the raw regime; `pending_remaining_days` is 0 while it equals the active regime. Persistence 1 activates on the first occurrence (the repository's loop needs two). Indicators are vectorised over the whole index; only the weekly persistence state is a loop |
| D33 | Gate exit and email | A gate that is not met exits 3 with a log line and no email. On the final scheduled attempt (the one before `signals.retryUntil`) it becomes a failed run with "no targets for week of {date}" in the digest. `placeholders: true` and an empty holidays list for the run year fail the run (exit 1, digest) instead. `selector.maxStaleTradingDays` supports only 0 |
| D34 | Target fields | Held-ticker prices (`ltp`) come from the newest holdings snapshot (fallback: book `avg_price`). A DROP for a ticker found in no bucket file has `bucket: null` and reason NOT_SELECTED. When the delta is unavailable, selected entries carry `status: null` |
| D35 | Secrets | Environment variables only, supplied by `docker run --env-file` from a host file outside the mounted data and config folders (template: `.env.example`). `app/analyst/secrets.py` rejects missing or placeholder values and never logs them |
| D36 | Broker client *(phase 2)* | Official `smartapi-python` SDK and `pyotp`, in an optional `analyst` dependency group so the other images are unaffected |

## Architecture

Stock Analyst in context · 3 read-only sources, 2 jobs, 2 outputs

Ledger is the only stage that talks to Angel One and it never places an order. Signals reads prices and bucket files, takes open positions from the ledger's book, and writes the one file the Risk Manager consumes. Neither stage writes to Ticker Data or Metadata folders.

## Deployment and orchestration

The Analyst runs on the same Windows host as the other two pipelines, as separate Docker containers started by Windows Task Scheduler. There is no cloud, queue or database.

| Property | Value |
|---|---|
| Language | Python 3.12+, package app/analyst/ |
| Image | New Dockerfile.analyst, built by CI as swing-trading-analyst with role batch (smoke-run python -m app.analyst.ledger --check, weekly scan, never deployed). Adds smartapi-python, pyotp, pandas, numpy, pyarrow to the base |
| Trigger | One Task Scheduler entry per stage, each a docker run --rm with the same bind mounts as the other pipelines |
| Volumes | C:\ProgramData\ticker-pipeline\data to /app/app/data, ...\config to /app/app/config (path still to confirm, as in the other TDDs) |
| Config | app/config/analyst.json; one-off app/config/seed_positions.csv; nse_calendar.json is shared and read-only |
| Secrets | Environment variables only: ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_MPIN, ANGEL_TOTP_SECRET, SMTP_USER, SMTP_PASSWORD. Never in files, never logged |
| Read-only inputs | app/data/market/ (prices, registry.csv, status.json, .lock), app/data/storage/*.csv (bucket files), app/config/config.json (bucket names). The Analyst never writes to them |
| Broker scope | Read endpoints only. The broker wrapper exposes no order or GTT call, so Angel One's static-IP order rules do not apply to this system |
| Own data | app/data/analyst/ (section "Data layout") |
| Run lock | app/data/analyst/.lock, shared by Ledger and Signals (section "Cross-cutting behaviour") |

**Daily order.** Ledger 16:30 IST (after NSE close, independent of the price pipelines). Metadata 19:30, Ticker Data Updator 21:00, then on Fridays Signals 21:30. The Ticker Data Archiver runs on the 2nd of Jan/Apr/Jul/Oct at 22:00 and the Metadata Cleaner on the 1st, so neither shares a slot with Signals.

**Task Scheduler entries**

- Ledger: Monday to Friday 16:30, repeat every 60 minutes until 20:30, do not start a new instance while one is running. Each attempt exits 0 immediately if today's ledger run already succeeded.
- Signals: Friday 21:30, repeat every 60 minutes for 48.5 hours (until Sunday 22:00), same rule. Each attempt exits 0 immediately if targets_{rebalance_date}.json already exists with status ok.
```
docker run --rm `
  -v C:\ProgramData\ticker-pipeline\data:/app/app/data `
  -v C:\ProgramData\ticker-pipeline\config:/app/app/config `
  -e ANGEL_API_KEY -e ANGEL_CLIENT_CODE -e ANGEL_MPIN -e ANGEL_TOTP_SECRET `
  -e SMTP_USER -e SMTP_PASSWORD `
  ghcr.io/debarpan-bose-chowdhury/swing-trading-analyst:latest python -m app.analyst.ledger
```

## Data layout and schemas

Raw broker snapshots are the only files that pile up, and they are purged after 90 days. Everything irreplaceable lives in three small files that are never purged.

```
app/config/
├── analyst.json
└── seed_positions.csv                  # one-off, read at the first Ledger run
app/data/analyst/
├── snapshots/{type}_{YYYY-MM-DD}.json   # type = tradebook | positions | holdings | funds; purged after 90 days
├── ledger/
│   ├── fills.csv                        # canonical fills, append-only, kept
│   └── book.csv                         # open tracked positions, kept
├── trading_journal.csv                  # closed positions, append-only, kept
├── regime/regime_history.csv            # rewritten by every Signals run
├── targets/targets_{rebalance_date}.json
├── backup/{YYYY-MM-DD}/                 # copies of fills.csv, book.csv, trading_journal.csv; 30 days
├── analyst_status.json
├── seed.done                            # written once after seeding
└── .lock
app/data/logs/
└── analyst_{stage}_{date}.log
```

Dates are IST dates, ISO YYYY-MM-DD. All files are written to a temp file and atomically renamed.

**ledger/fills.csv** (one row per broker fill, never edited)

| Column | Type | Rule |
|---|---|---|
| fill_key | string | Unique. Broker trade or fill id if present, else SHA-1 of order id, fill time, quantity and price |
| trade_date | date | Date of the fill |
| ticker | string | NSE symbol without series suffix |
| broker_symbol | string | As returned, for example TATASTEEL-EQ |
| side | enum | BUY or SELL |
| qty | int | Greater than 0 |
| price | float | INR per share |
| fill_time | string | ISO timestamp with +05:30, blank if the broker gives none |
| order_id | string | Broker order id if present |
| run_id | string | Ledger run that wrote the row |

**ledger/book.csv** (one row per open tracked position)

| Column | Type | Rule |
|---|---|---|
| ticker | string | NSE symbol, unique |
| qty | int | Shares held |
| avg_price | float | Average cost per share in INR |
| entry_date | date or UNKNOWN | Date of the first buy of the current position |
| entry_source | enum | FILLS, SEED, BROKER_AVG or UNKNOWN |
| last_reconciled | date | Last run that matched this row against holdings |

**snapshots/{type}_{date}.json** holds an envelope { "fetchedAt", "endpoint", "rows": [...] } with the broker's rows exactly as returned. The journal columns are in "Trading journal", the target file in "Target list".

## Broker session and end-of-day ledger

The ledger replaces the original "compare Old and New positions" step. Angel One holdings carry no buy date and the tradebook holds only the current trading day, so the only way to know entry and exit dates and prices is to record fills every day.

### Broker session

- **Library.** The official smartapi-python SDK behind a thin broker.py wrapper that exposes only login, holdings, positions, tradebook and funds. No order or GTT method is imported.
- **Login.** loginByPassword with client code, **MPIN** (accounts with a long password are told to switch to MPIN) and a TOTP generated by pyotp from ANGEL_TOTP_SECRET. One login per job; the JWT, refresh token and feed token stay in memory. Sessions end at midnight, so there is nothing to reuse tomorrow.
- **Invalid TOTP (AB1050).** Wait 60 seconds, generate a fresh code, up to 3 logins per run.
- **Token errors (AG8001, AG8002, AB8051).** One re-login, then fail the run.
- **Throttle.** At least 1.0 s between portfolio calls (the published limit is 1 per second for positions, holdings and tradebook). Request timeout 15 s.
- **Retryable errors.** Timeouts, AB1004, AB1021 and the plain-text "Access denied because of exceeding access rate" that Angel One returns when it wrongly rate-limits: 3 retries at 2, 4 and 8 seconds plus jitter.

### Run steps

- Take the run lock. If another stage holds it, exit 2 ("busy").
- If today's ledger already succeeded and --force is absent, exit 0.
- If ledger/book.csv does not exist, run the one-off seed (see "Trading journal").
- Log in, then fetch in this order: tradebook, positions, holdings, funds. A failed fetch does not stop the others.
- Write each successful response to snapshots/{type}_{today}.json. funds is stored for your reference only and never used in calculations, because several of its fields are known to come back empty.
- **Record fills.** Convert today's tradebook rows to canonical fills (DELIVERY product, NSE, tickers that pass the tracking filter below). Skip any fill_key already in fills.csv, append the rest, and apply them to book.csv in fill-time order (BUY before SELL when a fill has no time).
- **Reconcile** the book against holdings and positions (table below).
- **Journal.** Append rows for closed or reduced positions (see "Trading journal").
- **Housekeeping.** Copy fills.csv, book.csv and trading_journal.csv into backup/{today}/; delete backup folders older than 30 days; delete snapshot files older than 90 days, always keeping the newest file of each type.
- Write the ledger part of analyst_status.json, send the digest, release the lock.

### Tracking filter

A holding or fill is tracked when its tradingsymbol with the series suffix stripped (-EQ, -BE, -BZ and similar) equals a nse_symbol in the Ticker Data registry.csv and is not listed in capital.ignoreSymbols. Everything else is reported in the digest as **untracked** and is excluded from the book, the journal and Invested Capital. A rename or delisting therefore shows up as untracked rather than silently disappearing.

### Applying fills

- **BUY:** add the quantity and recompute the weighted average cost. If the book quantity was 0, entry_date is the fill date and entry_source is FILLS.
- **SELL:** one journal row per ticker per day, covering the day's total sold quantity at the day's weighted average price, valued against the book's average cost. The book quantity falls; the row is removed at 0. A partial sell keeps the remaining quantity at the same average cost and entry date.
- **SELL larger than the book quantity:** capped at the book quantity, the fill is still stored, and the digest flags the anomaly.

### Reconciliation

The observed quantity for a ticker is taken from holdings, falling back to the DELIVERY position's net quantity for shares bought today that holdings do not show yet. Angel One's exact T+1 behaviour (the t1quantity field) is UNVERIFIED, so the probe output decides the final rule before go-live.

| Condition after today's fills | Action | Journal effect |
|---|---|---|
| Observed equals book | Mark reconciled | None |
| Observed differs, and observed cost (quantity times average price) is within 1% of book cost | Treat as a corporate action (split or bonus): copy quantity and average price from the broker | None; digest line CORP_ACTION |
| Observed above book (a buy was missed) | Set quantity and average price from the broker; entry_date is the run date for a new ticker, unchanged otherwise; entry_source is BROKER_AVG | None; digest line MISSED_BUY |
| Observed below book (a sell was missed) | Reduce the book | One ESTIMATED row for the missing quantity: entry from the book, exit price is the ltp in the last good holdings snapshot, exit date is the run date |
| Ticker in the book but absent from holdings and positions | Observed quantity is 0, same as the row above | Same |

### Missed days

The ledger compares its last successful run date with today's date and the trading calendar, and lists every trading day in between that has no snapshot. Fills for those days cannot be fetched later, so the reconciliation above produces ESTIMATED journal rows and the digest lists them. You correct exit price and date from Angel One's contract note or P&L report; the next run notices the edit and promotes the row to MANUAL_VERIFIED.

### Status

ok means everything was fetched and reconciled. partial means at least one fetch failed but the run finished (a failed tradebook means today's fills are recovered by the next run's reconciliation). failed means login failed or nothing could be written; Task Scheduler's hourly retry covers it until 20:30.

## Trading journal

app/data/analyst/trading_journal.csv holds one row per closed or reduced position. The Analyst fills the auto columns; you own the manual columns, and the Analyst never touches them.

| Column | Type | Written by | Rule |
|---|---|---|---|
| trade_id | string | Analyst | {ticker}-{exit_date}-{n}, unique |
| ticker | string | Analyst | NSE symbol |
| qty | int | Analyst | Shares closed |
| entry_date | date or UNKNOWN | Analyst | From the book |
| entry_price | float | Analyst | Book average cost at the time of the sale |
| exit_date | date | Analyst | Fill date, or run date for an estimate |
| exit_price | float | Analyst | Weighted average sell price, or last known LTP for an estimate |
| pl | float | Analyst | (exit price minus entry price) times quantity, before charges |
| pl_pct | float | Analyst | (exit price divided by entry price minus 1) times 100, 2 decimals |
| est_charges | float | Analyst | Buy and sell charges plus DP from the cost model (section "Execution cost model") |
| net_pl | float | Analyst | pl minus est_charges |
| source | enum | Analyst | FILLS, ESTIMATED or MANUAL_VERIFIED |
| entry_source | enum | Analyst | FILLS, SEED, BROKER_AVG or UNKNOWN |
| auto_hash | string | Analyst | Hash of the auto values as written, used to detect your edits |
| run_id | string | Analyst | Run that wrote the row |
| notes, reason, tags | string | You | Never overwritten |

**Rules**

- **Append-only.** The Analyst adds rows and never deletes one. To write, it reads the file, appends, writes a temp file and renames it over the original, preserving every other cell exactly.
- **Correcting an estimate.** Edit the entry or exit fields of an ESTIMATED row. On its next run the Analyst sees that auto_hash no longer matches, recomputes pl, pl_pct, est_charges and net_pl, sets source to MANUAL_VERIFIED and refreshes the hash.
- **File open in Excel.** If the rename fails, the Analyst retries 3 times at 5 second gaps, then writes trading_journal.pending_{run_id}.csv beside it and says so in the digest. The next run merges any pending file first.

**Seeding at go-live.** Holdings carry no buy date, so positions you already hold have no recorded entry. Before the first Ledger run, fill app/config/seed_positions.csv with ticker,qty,entry_date,entry_price (from contract notes). When book.csv does not exist, the Ledger reads it once, creates book rows with entry_source = SEED, then writes seed.done. Tracked holdings missing from the seed file enter the book with entry_date = UNKNOWN, the broker's average price and entry_source = UNKNOWN, and the digest lists them for you to fix in the journal when they are sold. After seed.done exists the seed file is ignored.

## Regime classifiers

Both classifiers keep the maths of your classify_regime and calculate_regime_state exactly. This section only fixes their inputs, dates and guards. Every Signals run recomputes them from the full stored index history, so there is no carried state to drift.

### Inputs

The Close of the index named in regime.index (^NSEI), read from market/indices/fresh/NSEI.csv plus market/indices/archive/NSEI/*.parquet. The two tiers are concatenated, duplicate dates dropped (fresh wins) and the result sorted by date. Fewer than regime.minRows (210) rows is a hard failure.

### Raw regime (daily)

| Condition | Raw regime |
|---|---|
| Close above SMA200, and Close above SMA50, and 63-day return above 0 | BULL |
| Close above SMA200, and (Close at or below SMA50, or 63-day return at or below 0) | TREND |
| Close at or below SMA200, and Close above SMA50 | WEAK |
| Close at or below SMA200, and Close at or below SMA50 | BEAR |
| First 209 observations | Unknown |

SMA50 and SMA200 need 50 and 200 observations. The 63-day return is pct_change(63).

### Rebalance dates

- **History:** for each ISO week (Monday to Sunday) present in the index data, the last date in the series.
- **Live run:** the week's rebalance date is the last NSE trading day of the current ISO week according to nse_calendar.json, and the index series must contain a row for it. Weekends are always non-trading; holidays and special sessions come from the calendar.
- **Guard:** if the calendar's holidays list for the run year is empty, Signals refuses to run and alerts, because a holiday would otherwise look like a normal trading day. This is the situation the README warns about.
- A Saturday or Sunday retry belongs to the same ISO week, so it computes the same rebalance date as Friday.

### Active and pending regime (weekly)

The raw regime is sampled at rebalance dates only. A run of the same raw regime counts consecutive weeks.

| Output column | Meaning |
|---|---|
| raw_regime | Raw regime at the rebalance date |
| active_regime | BEAR activates at once. Any other regime activates on its persistenceWeeks-th consecutive occurrence. Before the first activation it is Unknown |
| pending_regime | Equals active_regime when raw matches it, otherwise the raw regime waiting to activate |
| pending_remaining_days | max(0, (persistenceWeeks - pending_count) * 7) calendar days |

persistenceWeeks is an integer of 1 or more from config. A raw regime missing on a rebalance date raises an error, as in your code.

### Output

regime/regime_history.csv (date, raw_regime, active_regime, pending_regime, pending_remaining_days), one row per rebalance date, rewritten on every run. --as-of YYYY-MM-DD reruns any past rebalance date and gives the same answer, because nothing is carried between runs.

## Stock selector

The selection rule is yours, unchanged: among stocks whose latest price is above their trend MA and whose momentum is positive, take the top_n with the highest momentum. What changes is the universe, the price series and the three fixes agreed in D18. Selection runs once per bucket, for the rebalance date only.

### Universe per bucket

- Bucket names come from the Metadata config.json (capBuckets), read only.
- For each bucket take the newest {Bucket}_{date}.csv in app/data/storage/ dated on or before the rebalance date. If it is older than selector.maxBucketFileAgeDays (7), Signals fails rather than trade on a stale universe.
- Keep symbols that exist in registry.csv with status active and have a stored price file. Inactive symbols are counted and excluded (fix c).

### Price series

The close_df for a bucket is a date-by-ticker table of **AdjClose** for that bucket's symbols, read from fresh/{Ticker}.csv and, when a configured lookback needs more rows than the fresh tier holds, the newest archive Parquet partitions. The rows needed are the largest of stock_trend_ma + 5 and lookback + 2 across the strategy table, plus a buffer of 5. The index regime uses the index's Close, as in D7. Rows after the rebalance date are dropped, so there is no look-ahead.

**No forward-fill.** A ticker with no stored row on a date has NaN there. A ticker without a row on the rebalance date is excluded and counted (fix b; selector.maxStaleTradingDays defaults to 0).

### Algorithm (per bucket, per regime)

| Step | Rule |
|---|---|
| 1 | Look up strategies[active_regime][bucket]. Missing, or regime Unknown, or top_n of 0 means no selection |
| 2 | Features, computed once for each distinct (lookback, stock_trend_ma) pair and passed to the selector (fix a). trend_ma is rolling(stock_trend_ma, min_periods=1).mean(); momentum is close / close.shift(lookback) - 1 |
| 3 | History gate on the number of rows up to the rebalance date: at least stock_trend_ma + 5 and at least lookback + 2, otherwise no selection |
| 4 | Candidates are tickers with momentum > 0 and latest price above trend_ma |
| 5 | Sort by momentum descending, ties by symbol ascending, keep top_n |

The symbol tie-break is new: the original sort leaves ties undefined, and ascending symbol is the tie rule the Metadata Filter already uses.

### Output per bucket

The selected tickers with rank, momentum, latest AdjClose and trend MA, plus counts of tickers excluded for each reason (inactive, noRowOnRebalanceDate, insufficientHistory, noPriceData). If more than selector.maxMissingShare (10%) of a bucket's universe has no row on the rebalance date, Signals fails without writing targets, because Ticker Data is probably still catching up; the hourly retry tries again.

### Known limitation

min_periods=1 means a ticker with fewer rows than stock_trend_ma gets a trend MA over a shorter window. This is kept from your original. The Metadata Filter already excludes tickers listed under 365 days, which makes it rare.

## Target list

The Risk Manager reads one file per rebalance: app/data/analyst/targets/targets_{rebalance_date}.json. It says what to hold and how that differs from what you hold today. It contains no quantities, no order prices and no sizing.

### Run gate

Signals writes targets only when every condition holds; otherwise it exits 3 ("gate not met") and the hourly retry tries again.

- market/status.json has stage = updator, status ok or partial, and lastTradingDay equal to the rebalance date.
- market/.lock does not exist (no Ticker Data run in progress).
- The index series contains a row for the rebalance date.
- The calendar guard from "Regime classifiers" passes and placeholders in analyst.json is false.

If no run succeeds by Sunday 22:00, the digest says "no targets for week of {date}". The Metadata health.json is not used.

### Delta rules

| Status | Meaning |
|---|---|
| KEEP | Selected now and held (in book.csv) |
| ADD | Selected now, not held |
| DROP | Held and tracked, not selected. Reason NOT_SELECTED, or NO_ALLOCATION when top_n for the active regime and bucket is 0 or missing, or UNKNOWN_REGIME when the active regime is Unknown |

Holdings come from ledger/book.csv as of the latest good Ledger run. If that run is older than signals.maxHoldingsSnapshotAgeDays (3), the file sets delta.available to false and omits KEEP and DROP rather than compare against stale holdings.

### Schema (schemaVersion 1)

```
{
  "schemaVersion": 1,
  "runId": "signals-2026-10-02T21:31:07+05:30",
  "generatedAt": "2026-10-02T21:31:12+05:30",
  "status": "ok",
  "rebalanceDate": "2026-10-02",
  "executionDate": "2026-10-05",
  "executionAt": "open",
  "regime": {
    "index": "^NSEI", "raw": "TREND", "active": "BULL", "pending": "TREND",
    "pendingRemainingDays": 7, "persistenceWeeks": 2
  },
  "capital": {
    "floatingInr": 250000.0, "investedCostInr": 410000.0,
    "investedMarketValueInr": 437500.0, "totalInr": 660000.0, "holdingsAsOf": "2026-10-02"
  },
  "composition": { "LargeCap": 0.5, "MidCap": 0.3, "SmallCap": 0.2 },
  "limits": { "maxPositionDrawdownPct": 0.17, "maxPortfolioDrawdownPct": 0.5 },
  "buckets": {
    "LargeCap": {
      "strategy": { "top_n": 10, "lookback": 126, "stock_trend_ma": 100 },
      "universe": 50,
      "excluded": { "inactive": 0, "noRowOnRebalanceDate": 1, "insufficientHistory": 0, "noPriceData": 0 },
      "selected": [
        { "ticker": "RELIANCE", "rank": 1, "momentum": 0.214, "price": 2987.4, "trendMa": 2801.2,
          "status": "KEEP", "estRoundTripCostInr": 412.0, "refNotionalInr": 120000.0 }
      ]
    }
  },
  "delta": {
    "available": true,
    "drop": [
      { "ticker": "XYZ", "bucket": "MidCap", "qty": 40, "avgCost": 512.3, "ltp": 498.1,
        "reason": "NOT_SELECTED", "estExitCostInr": 98.0 }
    ]
  },
  "untracked": ["ABCD"],
  "costModel": { "asOf": "2026-09-30", "minTradeNotionalInr": 20000 }
}
```

The values above are illustrative.

**Cost fields.** estRoundTripCostInr for an ADD or KEEP uses refNotionalInr (for ADD, costs.minTradeNotionalInr; for KEEP, the held market value). estExitCostInr for a DROP is the sell-side charges plus DP on the held quantity at LTP. Slippage is included at the bucket's bps (see "Execution cost model").

**Capital.** investedCostInr is the cost basis of tracked holdings (D21). floatingInr is the manual capital.floatingCapitalInr from config, re-read on every run. totalInr is their sum.

### Consumer rules

- Read analyst_status.json first. If app/data/analyst/.lock exists a run is in progress.
- A targets file is written once and never edited. A rerun for the same date is refused unless --force is given, in which case the old file is renamed targets_{date}.superseded_{time}.json.
- Files older than signals.targetsRetentionWeeks (104) are deleted at the end of each Signals run.

## Execution cost model

A round trip on Angel One delivery costs about 0.29% of the position before slippage: ₹293.28 on a ₹1 lakh buy and sell, of which STT is ₹200. The rates below come from Angel One's pricing page as read on 30 Sep 2026 and live in analyst.json, so they can be updated without a code change. Re-check them at go-live.

| Charge | Rate | Applies |
|---|---|---|
| Brokerage | Lower of ₹20 or 0.1% of the order, minimum ₹5, per executed order | Buy and sell |
| STT | 0.1% | Buy and sell |
| NSE transaction charge | 0.0030699% | Buy and sell |
| IPFT (NSE) | 0.0000001% as printed (UNVERIFIED, looks inconsistent) | Buy and sell |
| SEBI fee | ₹10 per crore (0.0001%) | Buy and sell |
| Stamp duty | 0.015% | Buy only |
| GST | 18% on brokerage plus transaction charge plus SEBI fee plus IPFT | Buy and sell |
| DP charge | ₹20 plus 18% GST, ₹23.60 per scrip per sell day | Sell only |

The quarterly AMC (₹60 plus GST, after the first trade of a quarter and free in year one) is not per trade and is left out.

**Worked example: ₹1,00,000 bought, then ₹1,00,000 sold at the same price**

| Item | Buy | Sell | Total |
|---|---|---|---|
| Brokerage (0.1% is ₹100, capped at ₹20) | 20.00 | 20.00 | 40.00 |
| STT | 100.00 | 100.00 | 200.00 |
| NSE transaction charge | 3.07 | 3.07 | 6.14 |
| SEBI fee | 0.10 | 0.10 | 0.20 |
| Stamp duty | 15.00 | 0.00 | 15.00 |
| GST | 4.17 | 4.17 | 8.34 |
| DP charge | 0.00 | 23.60 | 23.60 |
| Total | 142.34 | 150.94 | 293.28 |

A third-party calculator (knowyourbrokerage.in, rates dated 13 Jun 2026) gives ₹298.23 for a similar trade. The gap is mostly a different exchange-charge rate.

**Formula.** For a notional N, the estimated round-trip cost is buy charges plus sell charges plus DP plus 2 * slippageBps / 10000 * N. Slippage is per side and per bucket.

**Slippage defaults (heuristic, not sourced).** These are starting points for market-at-open or aggressive-limit orders; tune them from your own fills.

| Bucket | Slippage per side (bps) | Typical range |
|---|---|---|
| LargeCap | 10 | 5 to 10 |
| MidCap | 25 | 15 to 30 |
| SmallCap | 50 | 30 to 75 |

Double them on gap days. A minTradeNotionalInr of ₹20,000 is a starting value: at ₹20,000 the DP charge alone is about 0.12% of the trade.

**Where it is used.** The journal's est_charges and net_pl (charges only, no slippage, because the real fill price is already known) and the target list's estRoundTripCostInr and estExitCostInr (charges plus slippage).

## Cross-cutting behaviour

**Concurrency.** Ledger and Signals take app/data/analyst/.lock at start with an atomic create holding the PID and a timestamp. A stage that cannot get it exits 2 ("busy"). A lock older than 6 hours is stale and may be taken over. Signals only checks whether market/.lock exists; it never takes or writes the Ticker Data lock. Every file write goes to a temp file and is atomically renamed.

**Idempotency.** Rerunning Ledger on the same day is safe: fills are de-duplicated by fill_key and journal rows by the book state. Signals is idempotent per rebalance date (see "Target list").

**Rate limits and retries.** Broker calls follow "Broker session": at least 1.0 s apart, 3 retries at 2, 4 and 8 seconds with jitter, one re-login on token errors. Angel One has been seen rate-limiting a single call about every five minutes (forum reports through Aug 2026), so a retryable failure is never treated as a data problem.

**Time.** All dates and cutoffs are IST (fixed UTC+5:30), as in the Ticker Data README.

**Logging.** Each stage logs to stdout and app/data/logs/analyst_{stage}_{date}.log. Credentials, tokens and TOTP codes are never logged; broker rows are logged by count, not content.

**Alerts.** One digest email per run, through the same SMTP settings and credentials as Ticker Data. A failed or unconfigured send is logged and never fails the run.

| Stage | Digest contents |
|---|---|
| Ledger | Fetch result per endpoint, fills recorded, journal rows added, ESTIMATED rows awaiting your correction, missed trading days, CORP_ACTION and MISSED_BUY lines, untracked holdings, unseeded holdings, SELL-over-book anomalies, pending journal file, purge and backup result |
| Signals | Gate result, rebalance date, raw, active and pending regime, selected count per bucket, exclusion counts, KEEP, ADD and DROP counts, untracked list, stale-holdings warning, "no targets for week" if the window closes |

**Status file.** app/data/analyst/analyst_status.json holds one block per stage:

```
{
  "ledger":  { "status": "ok", "runDate": "2026-10-01", "finishedAt": "2026-10-01T16:32:40+05:30",
               "fillsRecorded": 2, "journalRowsAdded": 1, "estimatedRows": 0,
               "missedTradingDays": [], "untracked": [] },
  "signals": { "status": "ok", "runDate": "2026-10-02", "finishedAt": "2026-10-02T21:31:12+05:30",
               "rebalanceDate": "2026-10-02", "activeRegime": "BULL", "selectedCount": 18,
               "targetsFile": "targets/targets_2026-10-02.json" }
}
```

## Configuration

app/config/analyst.json holds every tunable. The strategy and composition numbers below are **illustrative placeholders, not recommendations**, and placeholders: true stops Signals from running until you replace them and set it to false.

```
{
  "placeholders": true,
  "broker": {
    "name": "AngelOne", "timeoutSeconds": 15, "minCallGapSeconds": 1.0,
    "maxRetries": 3, "backoffSeconds": [2, 4, 8],
    "loginAttempts": 3, "loginRetryGapSeconds": 60
  },
  "capital": { "floatingCapitalInr": 0, "ignoreSymbols": [] },
  "limits": { "maxPositionDrawdownPct": 0.17, "maxPortfolioDrawdownPct": 0.5 },
  "composition": { "LargeCap": 0.5, "MidCap": 0.3, "SmallCap": 0.2 },
  "rebalance": { "schedule": "weekly", "execution": "monday_open" },
  "regime": { "index": "^NSEI", "persistenceWeeks": 2, "minRows": 210 },
  "strategies": {
    "BULL":  { "LargeCap": { "top_n": 10, "lookback": 126, "stock_trend_ma": 100 },
               "MidCap":   { "top_n": 10, "lookback": 126, "stock_trend_ma": 100 },
               "SmallCap": { "top_n": 10, "lookback": 126, "stock_trend_ma": 100 } },
    "TREND": { "LargeCap": { "top_n": 5, "lookback": 63, "stock_trend_ma": 50 },
               "MidCap":   { "top_n": 5, "lookback": 63, "stock_trend_ma": 50 },
               "SmallCap": { "top_n": 5, "lookback": 63, "stock_trend_ma": 50 } },
    "WEAK":  { "LargeCap": { "top_n": 0, "lookback": 63, "stock_trend_ma": 50 },
               "MidCap":   { "top_n": 0, "lookback": 63, "stock_trend_ma": 50 },
               "SmallCap": { "top_n": 0, "lookback": 63, "stock_trend_ma": 50 } },
    "BEAR":  { "LargeCap": { "top_n": 0, "lookback": 63, "stock_trend_ma": 50 },
               "MidCap":   { "top_n": 0, "lookback": 63, "stock_trend_ma": 50 },
               "SmallCap": { "top_n": 0, "lookback": 63, "stock_trend_ma": 50 } }
  },
  "selector": { "maxStaleTradingDays": 0, "maxBucketFileAgeDays": 7, "maxMissingShare": 0.10 },
  "ledger": {
    "runTime": "16:30", "retryEveryMinutes": 60, "retryUntil": "20:30",
    "snapshotRetentionDays": 90, "backupRetentionDays": 30,
    "corpActionCostTolerance": 0.01
  },
  "signals": {
    "runTime": "21:30", "retryEveryMinutes": 60, "retryUntil": "Sun 22:00",
    "maxHoldingsSnapshotAgeDays": 3, "targetsRetentionWeeks": 104
  },
  "costs": {
    "asOf": "2026-09-30",
    "brokerage": { "flatInr": 20, "pct": 0.001, "minInr": 5 },
    "sttPct": 0.001, "nseTxnPct": 0.000030699, "ipftPct": 0.000000001,
    "sebiPct": 0.000001, "stampBuyPct": 0.00015, "gstPct": 0.18,
    "dpSellInr": 20, "dpGstPct": 0.18,
    "slippageBpsPerSide": { "LargeCap": 10, "MidCap": 25, "SmallCap": 50 },
    "minTradeNotionalInr": 20000
  },
  "paths": {
    "analyst": "app/data/analyst", "market": "app/data/market",
    "upstreamStorage": "app/data/storage", "metadataConfig": "app/config/config.json",
    "calendar": "app/config/nse_calendar.json", "seed": "app/config/seed_positions.csv",
    "logs": "app/data/logs"
  },
  "lock": { "staleAfterHours": 6 },
  "mail": { "smtpHost": "<set at deployment>", "smtpPort": 587,
           "sender": "<set at deployment>", "recipients": ["<set at deployment>"] }
}
```

**Validation at load** (any failure exits 1, which --check also reports)

| Field | Rule |
|---|---|
| placeholders | Must be false for Signals; Ledger ignores it |
| composition | Keys are a subset of the Metadata bucket names; values sum to 1.0 within 0.001 |
| limits | Each greater than 0 and at most 1 |
| regime.persistenceWeeks | Integer of 1 or more |
| strategies | Only the regimes BULL, TREND, WEAK, BEAR; every bucket named in composition present for every regime; top_n integer of 0 or more, lookback and stock_trend_ma integers of 1 or more |
| rebalance | Only weekly and monday_open are accepted in this version. monday_open means the first NSE trading day after the rebalance date, at the open |
| capital.floatingCapitalInr | Number of 0 or more, re-read on every run |
| costs | Every rate a number of 0 or more |

## Failure modes

| Failure | Behaviour |
|---|---|
| Angel One login fails after 3 attempts | Ledger exits non-zero with status failed; Task Scheduler retries hourly until 20:30; if none succeeds, the next run reconciles by diff and writes ESTIMATED rows |
| Invalid TOTP (AB1050) | Wait 60 s, fresh code, up to 3 logins |
| Token error mid-run (AG8001, AG8002, AB8051) | One re-login, then the run fails |
| Rate-limit text, AB1021, AB1004, timeout | 3 retries at 2, 4, 8 s with jitter; then that endpoint is marked failed and the run is partial |
| Tradebook fails, holdings succeed | Snapshots saved; status partial; today's fills are recovered by the next run's reconciliation as ESTIMATED rows |
| Holdings fails, tradebook succeeds | Fills recorded; reconciliation skipped; status partial |
| Tradebook field names differ from the parser | Ledger fails fast naming the unmapped field and keeps the raw snapshot; run the probe and fix the mapping |
| One or more ledger days missed | Reconciliation by diff, ESTIMATED rows, digest lists the dates |
| SELL larger than the book quantity | Quantity capped, fill still stored, anomaly in the digest |
| Holding not in the registry, or renamed | Reported as untracked; excluded from book, journal and capital |
| Split or bonus in holdings | Book adjusted from the broker; no journal row; CORP_ACTION in the digest |
| Journal file locked by another program | 3 retries at 5 s, then a pending file and a digest note |
| Ticker Data gate not met | Signals exits 3; hourly retry; "no targets for week" after Sunday 22:00 |
| Index has fewer than 210 rows, or no raw regime on a rebalance date | Signals fails, no targets written |
| Holidays list empty for the run year | Signals refuses to run and alerts |
| placeholders still true | Signals refuses to run and alerts |
| Bucket file older than 7 days | Signals fails, no targets |
| More than 10% of a bucket has no row on the rebalance date | Signals fails and retries hourly |
| Holdings snapshot older than 3 days | Targets written with delta.available false |
| Lock held by another stage | Exit 2 "busy"; a lock older than 6 h is taken over |
| Email send fails | Logged; run result unaffected |
| Disk loss | The 30-day backup lives on the same disk, so fills, book and journal are lost. ⚠ Back up the host folder separately |

## Known limitations and risks

- **Entry and exit accuracy depends on the daily run.** Angel One gives no buy date and keeps only today's trades. A missed day produces ESTIMATED journal rows with last-known-LTP exit prices until you correct them.
- **Broker field semantics are UNVERIFIED.** The tradebook fill field names, how t1quantity behaves around T+1 settlement, and the pledged-quantity fields are not documented in anything that could be fetched. The probe and the reconciliation rule are the safeguard.
- **Angel One's rate limiter misfires.** Forum reports through Aug 2026 describe rejections at about one call every five minutes, clustered 09:15 to 10:30 IST. Evening runs and retries are the mitigation.
- **Unattended TOTP login.** Angel One has no published statement allowing or forbidding scripted daily logins, and you chose automation anyway (D16). If Angel One changes its login, the Ledger stops and the digest says so.
- **The backup is not disaster recovery.** It guards against a bad write or an accidental edit, not disk loss.
- **Corporate-action detection is a heuristic.** A 1% cost tolerance can misclassify a real trade as a split, or the reverse. The digest line lets you check.
- **Adjusted prices depend on Yahoo**, inherited from Ticker Data. Features are recomputed from full history each run, so a restated AdjClose is picked up automatically.
- **Short price histories.** The trend MA uses min_periods=1 (kept from your original), so a ticker with fewer rows than the MA window gets a shorter window.
- **The 50% portfolio drawdown limit** is kept as you wrote it. It is three times looser than the 17% position limit; the Risk Manager enforces both.
- **The holiday calendar needs upkeep.** Signals refuses to run for a year whose holidays list is empty, and a wrong list gives a wrong rebalance date.
- **One account, one client code.** Nothing here supports several accounts.
- **Orders are a later problem, with rules.** From 1 Apr 2026 Angel One accepts API orders only from a whitelisted static IPv4 address and says market and IOC orders are prohibited for algorithms. The future execution component must use limit orders; this system places none.

## Inputs needed before go-live

- Create a SmartAPI Trading API key in Angel One's developer portal. Community reports say the key form may ask for an IP address even for read-only use (UNVERIFIED); enter the host's public IPv4 address. Some new-style keys have returned AG8004 Invalid API Key on data endpoints, so the probe must pass before anything is scheduled.
- Set ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_MPIN, ANGEL_TOTP_SECRET, SMTP_USER and SMTP_PASSWORD on the host and confirm the 4-digit MPIN works for API login.
- Run python -m app.analyst.probe --check-broker and check the tradebook fill fields, what quantity and t1quantity mean in holdings, and that holdings carry no buy date. Fix the parser mapping and the reconciliation rule from its output.
- Replace every placeholder in analyst.json with real values (persistence weeks, strategies per regime and bucket, composition), put the real floating capital in, and set placeholders to false.
- Fill holidays and specialSessions for the current year in nse_calendar.json. The Ticker Data README says it ships empty.
- Fill seed_positions.csv from your contract notes for every position you already hold.
- Set the SMTP host, sender and recipients in analyst.json.
- Confirm the bind-mount path C:\ProgramData\ticker-pipeline, still flagged "confirm" in the other TDDs.
- Re-check the charge rates on Angel One's pricing page, and confirm the IPFT rate, which prints as 0.0000001%.
- Decide how the host folder is backed up outside this system.

## Rules fixed while writing (not asked; change them if you disagree)

- Ledger retries hourly until 20:30 and exits early once today has succeeded; Signals retries hourly until Sunday 22:00.
- Same-day fills are applied in fill-time order, BUY before SELL when a fill has no time. Fills are de-duplicated by fill_key.
- Observed quantity for reconciliation is taken from holdings, falling back to the DELIVERY position's net quantity; the probe may change this.
- Corporate-action tolerance is 1% of cost.
- An estimated exit uses the last good snapshot's LTP and the run date.
- Snapshot purge always keeps the newest file of each type.
- Backup copies the three ledger files after each successful Ledger run and keeps 30 days.
- Stale-input limits: bucket file 7 days, holdings snapshot 3 days, missing price rows 10% of a bucket.
- An Unknown active regime writes targets with every tracked holding as DROP, reason UNKNOWN_REGIME.
- Selection ties break on symbol ascending.
- The journal carries est_charges and net_pl beyond the columns you listed.
- ADD cost estimates use minTradeNotionalInr as the reference notional; DROP estimates use the held quantity at LTP.
- The official smartapi-python SDK and pyotp are used, behind a read-only wrapper.
- Stage code reuses the mailer, atomic-write, IST-time and calendar helpers from app/market/.
- Target files are kept 104 weeks.

## Sources

Angel One facts come from a research pass dated 30 Sep 2026. Angel One's official docs pages refuse automated access, so those facts come from the official SDK, the official forum and Angel One's own notices. Sources marked community are user reports.

- (official): static IP for orders, market and IOC orders prohibited for algorithms
- (official): login, tokens, headers, endpoints
- (official moderator): per-endpoint rate limits
- (official): holdings fields
- (community, Aug 2026)
- (official admin): static-IP scope, midnight logout, 9 orders per second
- (official): retail algo framework
- (official): brokerage, STT, exchange, SEBI, stamp, DP
- (third party): cross-check of the worked example

Internal inputs: your Stock Analyst notes, the NSE Ticker Metadata Pipeline TDD, the Ticker Data System TDD and the Swing-Trading-System README.
