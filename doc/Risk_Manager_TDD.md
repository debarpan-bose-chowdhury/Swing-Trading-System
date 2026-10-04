# Risk Manager — Technical Design Document

Oct 2, 2026 · @Deba · Status: Built (see "As-built decisions")

The Risk Manager turns the Stock Analyst's weekly target list and your Angel One ledger into daily and weekly **signals**: what to buy, what to sell, how many shares, and why. It keeps your account's risk inside limits you set, and it measures how the strategy is doing. It places no orders and never talks to the broker.

## Overview

The Risk Manager reads four sources and writes one signal file per trading day. It reads the Analyst's target list and ledger book, the Ticker Data prices, the Metadata bucket files and the NSE surveillance lists it downloads itself. A small Evaluator also writes your NAV history, a shadow portfolio and a weekly statistics report.

Four logical blocks replace the original three. They share code but have separate responsibilities, so a failing report can never block a stop-out.

| Block | Replaces | Job |
|---|---|---|
| Sizer | Entry Calculator | Turns the Analyst's selected names into share quantities (risk-based, capped), once a week |
| Risk Monitor | Actor (decision part) | Every day: trailing stops, drawdown ladder, exposure caps, surveillance exits, tax deferral, cooldown |
| Signal Writer | Actor (output part) | Merges Monitor and Sizer results into one `signals_{asOf}.json` |
| Evaluator | Evaluator | Reporting only: daily NAV for the actual and shadow books, statistics, tax estimate. Never in the decision path |

| Stage | Command | Trigger | Purpose |
|---|---|---|---|
| Surveillance | python -m app.risk.surveillance | Task Scheduler, Mon to Fri 20:15 IST, hourly retry until 22:15 | Download NSE ASM, GSM, trade-for-trade and price-band lists, normalise, store |
| Run | python -m app.risk.run | Mon to Thu 21:45 IST, hourly retry until 08:00 next day; Friday 21:45, hourly retry until Monday 08:00 | Gate, NAV, Monitor, Sizer (rebalance days only), write the signal file, update the shadow portfolio |
| Evaluate | python -m app.risk.evaluate | Mon to Fri 22:30 IST, hourly retry until 08:00 next day | Append NAV rows, write statistics every Friday, tax estimate, digest |
| Probe | python -m app.risk.probe --check-nse | Manual, before go-live and after any NSE site change | Fetch the candidate NSE list pages and print the file locations and column names found |

Every stage accepts --check (load config, prove imports, print `<stage>: check ok`, no network, no writes), like the other stages in the repo.

**Signal only.** Whoever acts on a signal, you today or an execution component later, decides order type, limit price and timing. The Risk Manager exposes no order, GTT or broker call and holds no Angel One secret. Angel One's static-IP, limit-only and rate-limit rules therefore do not apply to this system. They will apply to the future executor (see "Known limitations").

**Out of scope:** order placement and any order-intent or broker-facing file, backtesting and parameter optimisation, the Analyst, Ticker Data and Metadata systems themselves (only their read contracts are used here), multi-account support, intraday monitoring.

**Sources of this design:** your original Risk Manager notes, the Stock Analyst, Ticker Data and Metadata TDDs, the repo README, a research pass dated 2 Oct 2026 (risk practice for Indian momentum portfolios, SEBI/NSE algo and market-structure rules, tax, performance analytics) and 32 clarification questions answered by you. Anything not confirmed is marked UNVERIFIED.

## Decision log

Every point where the original notes were unclear, conflicting or unsafe was put to you. "Proposed" means the redesign offered was chosen over the original. "Kept" means the original stands. "Yours" means you chose something other than the offered options.

| # | Topic | Original design | Decision |
|---|---|---|---|
| D1 | Scope and execution | Actor closes positions, destination Null; Analyst TDD assigns order placement to "the Risk Manager and execution components" | **Signals only, no execution, no order-intent file, no broker access.** Output is a signal file with quantity, reference price and reason (Yours) |
| D2 | Block structure | Entry Calculator, Evaluator, Actor chain; Evaluator feeds Actor | **Four blocks:** Sizer, Risk Monitor, Signal Writer, and a reporting-only Evaluator outside the decision path (Proposed) |
| D3 | Position exit rule | Close a position on its drawdown, limit 17%, baseline undefined, same number for every bucket | **Trailing ATR stop by bucket:** highest close since entry minus 3.5 x ATR20, clamped per bucket, close-based, executed next session (Proposed) |
| D4 | Portfolio limit | Close the complete holding at 50% drawdown | **Graded exposure ladder** from peak NAV index: -10 / -15 / -20 / -25% to 75 / 50 / 25 / 0% invested, manual restart after flat (Proposed) |
| D5 | Signal content | Entry Calculator and Actor each produce something, no unified output | **One action file:** ticker, side, quantity, reference price, reason code and rule trace. No order type or limit price (Proposed) |
| D6 | Base capital | "Get the base capital", undefined | **NAV at market value:** tracked holdings at latest raw Close plus ledger cash (Proposed) |
| D7 | Weight per symbol | "Get weight per symbol", undefined | **Risk-based with caps:** 1.25% of NAV risked to the stop, per-name caps, unused amount stays cash (Proposed) |
| D8 | Re-entry | "Do not enter the same position closed yesterday" | **Cooldown after STOP exits only:** 10 trading days, then re-entry only if re-selected and the close is above the stop-day close (Proposed) |
| D9 | Regime and exposure | Not stated | **Configurable max-invested table per regime, shipped at 100% everywhere**, so your strategy is unchanged until you tune it (Proposed) |
| D10 | Ladder levels | Not stated | **-10/-15/-20/-25% to 75/50/25/0%**, step re-risking one rung per week under conditions, **manual restart** from flat (Proposed) |
| D11 | Evaluated portfolio | "Daily change in the holding positions" | **Both** your actual book and a **shadow portfolio** that follows every signal, side by side with a tracking gap (Yours) |
| D12 | Reference index | "The reference index", unnamed | **^NSEI only**, as the regime index; reports state it is a price index (Kept) |
| D13 | Cash | Not stated | **Cash ledger** from fills plus a deposit/withdrawal/dividend file you edit (Proposed) |
| D14 | Held-name resizing | Not stated | **No-trade band, then resize:** only when off target by more than max(25% of target, 2.5 percentage points) and the trade is at least Rs 10,000 (Proposed) |
| D15 | Cash after a stop-out | Not stated | **Stays cash until the next Friday cycle.** No intraweek buying (Proposed) |
| D16 | Ladder cut order | Not stated | **Highest-risk first:** largest stop distance as a share of NAV, ties SmallCap, MidCap, LargeCap (Proposed) |
| D17 | Sizing numbers | None given | **Research defaults** (section "Sizer"), all in `risk.json` (Proposed) |
| D18 | Shadow fills | Not stated | **Next raw Open plus the Analyst's slippage bps plus its cost model**, starts from your NAV at go-live (Proposed) |
| D19 | Market-rule checks | Not stated | **ADV participation cap and daily NSE surveillance lists in v1** (ASM, GSM, trade-for-trade, band at or below 5%). You chose the larger option (Yours) |
| D20 | Failure behaviour | Not stated | **Same gating as the Analyst:** exit 3, hourly retry, no file, failed-run digest on the final attempt. Nothing is liquidated for a technical fault (Proposed) |
| D21 | Surveillance actions | Not stated | **Block entries on any flag; exit on GSM or trade-for-trade only;** ASM and tight bands on held names warn (Proposed) |
| D22 | Surveillance list failure | Not stated | **Block entries, still evaluate exits** (cached list up to 3 trading days old) (Proposed) |
| D23 | Schedule | Not stated | **Three commands** (surveillance 20:15, run 21:45, evaluate 22:30) plus a probe (Proposed) |
| D24 | Stop price basis | Not stated | **AdjClose** for ATR and highs, raw Close only to value positions; UNKNOWN entry dates start at the first Risk Manager run (Proposed) |
| D25 | Analyst `limits` (17% / 50%) | Kept and passed through by the Analyst (its D17) | **Retired.** The Risk Manager ignores `limits` in the target file. `risk.json` is the only source (Proposed) |
| D26 | Concentration | Not stated | **Portfolio heat cap only.** Sector and correlation are not enforced (Proposed) |
| D27 | Tax | Not stated | **Defer rank-based DROPs near 12 months** under D29's conditions (Yours) |
| D28 | Evaluator output | "Include the one that you feel important" | **Daily NAV file, weekly statistics report** (Proposed) |
| D29 | Deferral conditions | Not stated | **Within 28 days of 12 months, unrealised gain at least 10%, close above the strategy's trend MA, entry date known.** NOT_SELECTED drops only (Proposed) |
| D30 | Tax reporting | Not stated | **Realised short and long-term gains per financial year, estimated tax, post-tax return, near-12-month list** (Proposed) |
| D31 | Deployment | Not stated | **Same pattern, new image** `swing-trading-risk`, package `app/risk/`, SMTP only (Proposed) |
| D32 | Anything else | — | Nothing further. Minor points fixed while writing are listed at the end |

**What changed against the original text, in one place.** The original `Entry Calculator` read the same list twice (steps 1 and 2) and its source was the old "Stock Selector"; the source is now the Analyst target file. "Get the base capital" and "weight per symbol" are defined (D6, D7). The Evaluator's statistics are kept and extended (D28) but no longer gate the Actor. The Actor's "close the individual position or the complete holding" is replaced by the stop (D3) and the ladder (D4). "Do not enter the same position closed yesterday" is replaced by D8.

**Kept from the original, unchanged:** the idea of a daily evaluator with bear-day statistics, compound P/L over the bear period, maximum drawdown per symbol, strategy and reference index, yearly returns, alpha and dominant regime; the Risk Manager as the only component that interprets drawdown; weekly cadence with Monday-open execution (set by the Analyst).

## Architecture

```
 Ticker Data (read)        Metadata buckets (read)      Analyst (read)                 NSE (download)
 market/fresh,archive      storage/{Bucket}_{d}.csv     targets_{d}.json               ASM / GSM / T2T /
 market/status.json        config.json (names)          ledger/book.csv, fills.csv     price-band lists
 indices/NSEI              regime/regime_history.csv    trading_journal.csv
                           analyst_status.json          analyst.json (costs, read-only)
          │                        │                          │                              │
          └────────────┬───────────┴──────────────┬───────────┘                              │
                       ▼                          ▼                                          ▼
              ┌───────────────────────────────────────────────┐              ┌─────────────────────────┐
              │ Run (21:45)                                   │◀─ lists ─────│ Surveillance (20:15)    │
              │  gate → NAV → Risk Monitor → Sizer (Fridays)  │              └─────────────────────────┘
              │  → Signal Writer → shadow portfolio update    │
              └───────────────┬───────────────────────────────┘
                              ▼
          app/data/risk/signals/signals_{asOf}.json   ← the file you (or a later executor) read
                              │
                              ▼
              ┌───────────────────────────────────────────────┐
              │ Evaluate (22:30)  reporting only              │
              │  NAV actual + shadow, statistics, tax, digest │
              └───────────────────────────────────────────────┘
```

Surveillance writes only its own folder. Run reads everything above and writes signals, state and shadow files. Evaluate never changes signals or state that Run reads, except `nav_*.csv`, which only Evaluate and Run's own NAV step write (see "NAV and cash"). No stage writes to Ticker Data, Metadata or Analyst folders.

## Deployment and orchestration

The Risk Manager runs on the same Windows host as the other pipelines, as separate Docker containers started by Windows Task Scheduler. There is no cloud, queue or database.

| Property | Value |
|---|---|
| Language | Python 3.12+, package app/risk/ |
| Image | New Dockerfile.risk, built by CI as swing-trading-risk with role batch (smoke-run python -m app.risk.run --check, weekly scan, never deployed). Adds pandas, numpy, pyarrow, requests to the base. No SmartAPI SDK |
| Trigger | One Task Scheduler entry per stage, each a docker run --rm with the same bind mounts as the other pipelines |
| Volumes | C:\ProgramData\ticker-pipeline\data to /app/app/data, ...\config to /app/app/config (path still to confirm, as in the other TDDs) |
| Config | app/config/risk.json; user-edited app/config/cash_flows.csv; nse_calendar.json and analyst.json are shared and read-only |
| Secrets | Environment variables only: SMTP_USER, SMTP_PASSWORD, via --env-file from the same host folder as the Analyst (outside the mounted folders). No Angel One variables |
| Read-only inputs | app/data/market/ (prices, registry.csv, status.json, .lock), app/data/storage/*.csv, app/config/config.json (bucket names), app/data/analyst/ (targets, ledger, journal, regime history, analyst_status.json), app/config/analyst.json (costs block) |
| Own data | app/data/risk/ (section "Data layout") |
| Run lock | app/data/risk/.lock, shared by all three stages |

**Daily order.** Ledger 16:30, Surveillance 20:15, Metadata 19:30, Ticker Data Updator 21:00, (Fridays) Analyst Signals 21:30, Risk Manager Run 21:45, Evaluate 22:30. The Updator can overrun its slot. Run's gate checks Ticker Data's status file rather than the clock, so an overrun only delays Run by an hourly retry.

**Task Scheduler entries**

- Surveillance: Monday to Friday 20:15, repeat every 60 minutes until 22:15, do not start a new instance while one is running. Each attempt exits 0 once today's normalised list exists.
- Run: Monday to Thursday 21:45, repeat every 60 minutes for 10.25 hours (until 08:00). Friday 21:45, repeat every 60 minutes for 58.25 hours (until Monday 08:00). Each attempt exits 0 immediately if `signals_{asOf}.json` already exists with status ok.
- Evaluate: Monday to Friday 22:30, repeat every 60 minutes until 08:00. Exits 0 once the NAV row for the latest asOf exists.

```
docker run --rm `
  -v C:\ProgramData\ticker-pipeline\data:/app/app/data `
  -v C:\ProgramData\ticker-pipeline\config:/app/app/config `
  --env-file C:\ProgramData\ticker-pipeline\secrets\analyst.env `
  ghcr.io/debarpan-bose-chowdhury/swing-trading-risk:latest python -m app.risk.run
```

## Data layout and schemas

```
app/config/
├── risk.json
└── cash_flows.csv                          # you edit: opening cash, deposits, withdrawals, dividends
app/data/risk/
├── surveillance/
│   ├── raw/{source}_{YYYY-MM-DD}.*          # as downloaded, purged after 30 days
│   └── surveillance_{YYYY-MM-DD}.json       # normalised, kept 90 days
├── state/
│   ├── positions.csv                        # per held ticker: bucket, track_start
│   ├── cooldown.csv
│   ├── deferred_drops.csv
│   └── ladder_state.json
├── signals/signals_{asOf}.json              # the output
├── nav/
│   ├── nav_actual.csv                       # one row per asOf, kept
│   ├── nav_shadow.csv                       # kept
│   └── positions_daily.csv                  # per-position daily P/L, kept
├── shadow/
│   ├── book.csv   cash.csv   fills.csv      # simulated, kept
│   ├── state/{positions,cooldown,deferred_drops}.csv, ladder_state.json
│   └── signals/signals_{asOf}.json
├── reports/
│   ├── stats_{YYYY-MM-DD}.json              # every Friday
│   └── tax_{FY}.json                        # rewritten by every Friday run
├── backup/{YYYY-MM-DD}/                     # copies of state/, nav/, shadow/ ; 30 days
├── risk_status.json
└── .lock
app/data/logs/
└── risk_{stage}_{date}.log
```

Dates are IST dates, ISO YYYY-MM-DD. All files are written to a temp file and atomically renamed.

**state/positions.csv** (one row per held tracked ticker)

| Column | Type | Rule |
|---|---|---|
| ticker | string | NSE symbol, unique |
| bucket | string | Last bucket this ticker was found in (see "Bucket of a held ticker") |
| track_start | date | First date the stop replay starts. entry_date from the book when known, else the first asOf the Risk Manager saw the position |
| track_source | enum | ENTRY_DATE or FIRST_RUN |
| last_seen | date | Last asOf the ticker was in the book |

**state/cooldown.csv**

| Column | Type | Rule |
|---|---|---|
| ticker | string | Unique |
| trigger_date | date | asOf of the STOP signal |
| trigger_adj_close | float | AdjClose on trigger_date |
| release_after | date | trigger_date plus `cooldown.stopTradingDays` trading days |

Rows are removed once the ticker has been re-selected and passed, or after 52 weeks.

**state/deferred_drops.csv**

| Column | Type | Rule |
|---|---|---|
| ticker | string | Unique |
| drop_date | date | Rebalance date of the DROP that was deferred |
| anniversary | date | entry_date plus 12 months |
| entry_date | date | From the book |

**state/ladder_state.json**

```
{ "rung": 0, "peakIndex": 1.0, "peakDate": "2026-10-02", "baselineIndex": 1.0, "flatLocked": false,
  "flatLockedSince": null, "lastRestartFrom": null, "lastReRiskWeek": null, "shadowStartDate": null }
```

`rung` is 0 (fully allowed) to 4 (flat). `peakIndex` and `baselineIndex` are on the time-weighted NAV index (see "NAV and cash").

**nav/nav_actual.csv and nav/nav_shadow.csv**

| Column | Type | Rule |
|---|---|---|
| date | date | asOf, unique |
| positions_value | float | Sum of qty times raw Close of tracked holdings |
| cash | float | Ledger cash (section "NAV and cash") |
| nav | float | positions_value plus cash |
| flow | float | External cash flow of the day from cash_flows.csv (deposit positive) |
| twr_index | float | Time-weighted NAV index, 1.0 on the first row |
| bench_close | float | Benchmark close (^NSEI) |
| active_regime | string | From the Analyst's regime_history, carried forward daily |
| rung | int | Ladder rung after this run |

**nav/positions_daily.csv** holds date, ticker, qty, raw close, value, day P/L, day return and drawdown from the high-water mark, for actual holdings only.

**app/config/cash_flows.csv** (you edit; the Risk Manager never writes it)

| Column | Type | Rule |
|---|---|---|
| date | date | The day the cash moved |
| type | enum | OPENING, DEPOSIT, WITHDRAWAL, DIVIDEND or OTHER |
| amount_inr | float | Positive for money in, positive for WITHDRAWAL as well (the type gives the sign) |
| note | string | Free text |

One OPENING row is required: the cash balance of the tracked universe at the start of the ledger. DIVIDEND rows are your record of dividends received, because the Ledger does not capture them.

## Run gate

Run writes signals only when every condition holds. Otherwise it exits 3 ("gate not met") and the hourly retry tries again. A gate that is not met sends no email until the final attempt.

- `asOf` is the latest NSE trading day (calendar) with a final bar. `market/status.json` has status ok or partial, `lastTradingDay` equal to asOf when it is set (the Archiver also writes this file without that field; then the index-row and missing-share checks decide, as in Analyst D25), and `market/.lock` does not exist.
- The ^NSEI series has a row for asOf.
- The Analyst's ledger succeeded on asOf: `analyst_status.json` ledger block has status ok or partial and `lastGoodRunDate` equal to asOf (`gate.maxLedgerLagTradingDays`, default 0). A partial run with a failed holdings fetch adds a warning, because the book is then unreconciled.
- At most `gate.maxMissingShare` (10%) of the tickers needed this run (held plus, on rebalance days, selected) lack a row for asOf. Tickers without a row are skipped with a warning.
- The calendar's holidays list for the run year is not empty.
- `risk.json` validates (below). `placeholders` is false.
- On a rebalance day (asOf is the last Monday to Friday trading day of its ISO week, Analyst D26): `targets/targets_{asOf}.json` exists with status ok and schemaVersion 1. If it does not exist, attempts before `gate.targetsWaitUntil` (Sunday 22:00) exit 3. The first attempt after that time continues **without targets**: Monitor only, `weekly.included` false, "no targets for week of {asOf}; exits only" in the digest. Stop-outs are therefore never held back by a failed Analyst run beyond the Sunday deadline.

On any non-trading day Run exits 0 ("no new bar"). A rerun for an asOf that already has an ok signal file is refused unless `--force`, in which case the old file is renamed `signals_{asOf}.superseded_{time}.json`.

On the final attempt (the one before `gate.retryUntil`), a gate that is still not met becomes a failed run with "no signals for {asOf}" in the digest. Nothing is ever liquidated because of a technical fault.

## NAV and cash

**Tracked universe.** NAV covers the tracked holdings in `ledger/book.csv` (registry tickers not in `ignoreSymbols`, Analyst D14) and the ledger cash. Untracked holdings and cash movements are outside NAV.

**Positions value.** Sum over the book of qty times the raw `Close` of asOf. A held ticker with no row for asOf is valued at its last available Close and flagged. Raw Close is used for value because that is what the broker shows; AdjClose is used only for signals (D24).

**Cash ledger.** From the OPENING row's date forward:

cash(asOf) = opening cash + deposits + dividends - withdrawals + sum of SELL fills (qty x price, minus estimated charges) - sum of BUY fills (qty x price, plus estimated charges)

with fills from `ledger/fills.csv` dated on or after the opening date, and charges from the Analyst's cost model (`analyst.json` costs block, read-only; charges only, no slippage because the fill price is real). The digest shows `cashDriftInr` only when `cash.reconcileField` names a broker funds field that the Analyst probe has confirmed as reliable; otherwise no reconciliation is done.

**Time-weighted index.** twr_index(t) = twr_index(t-1) x (nav(t) - flow(t)) / nav(t-1). Flows are treated as arriving at the end of the day. Drawdown, the peak, and re-risking conditions all use twr_index, so a deposit never looks like a gain and a withdrawal never looks like a loss.

**Jump check.** A daily NAV change of more than 15% with no flow row raises a NAV_JUMP warning (a corporate-action timing mismatch between the Ledger at 16:30 and the Updator at 21:00 is the usual cause). It never blocks a run.

## Stop engine

Applies to every tracked held position, every run, on AdjClose.

**Adjusted prices.** For each day, f = AdjClose / Close. Adjusted High, Low and Close are High x f, Low x f and AdjClose. True range uses the adjusted values. ATR20 is the simple mean of the last 20 true ranges (`stops.atrMethod`: sma). A position needs at least 21 rows of history after adjustment, otherwise it has no ATR and the stop uses the bucket's upper clamp (the widest) with a warning.

**Replay.** Every run replays the position from `track_start` to asOf, with no carried stop state:

```
hwm(t)   = max(AdjClose from track_start to t)
width(t) = max( lo_b x hwm(t), min( k x ATR20(t), hi_b x hwm(t) ) )      # rupees; lo_b, hi_b from stops.clampPct for the bucket
stop(t)  = max( stop(t-1), hwm(t) - width(t) )                          # ratchet: a stop never moves down
```

The first stop on `track_start` has no previous value. A STOP trigger on date t is AdjClose(t) at or below stop(t). A bar on `track_start` itself is part of the high-water mark but cannot trigger.

**Worked example.** SmallCap, k = 3.5, clamp 18% to 28%, ATR20 = Rs 9, highest AdjClose since entry = Rs 300. 3.5 x 9 = 31.5, which is below 18% of 300 = 54, so the width is 54 and the stop is 300 - 54 = **Rs 246**. If the stock later closes at Rs 245, the Monitor signals SELL for the whole position.

**What is checked each run.** All bars after `lastGoodAsOf` (the previous successful Run's asOf) up to asOf, not just the last bar. A breach on a skipped day is reported as a STOP with `lateBreach` true. A STOP signal repeats on every run until the book no longer holds the ticker.

**Stop width for sizing.** The Sizer uses the nominal width, clamp(k x ATR%, lo_b, hi_b) as a fraction of price, not the distance left to a ratcheted stop. This stops a top-up from growing as the stop nears.

**Bucket of a held ticker.** From the newest `{Bucket}_{date}.csv` dated on or before asOf. If none lists the ticker (it dropped out of the top N), the bucket stored in `state/positions.csv` is kept. If the ticker was never in a bucket file, `stops.bucketFallback` (SmallCap) is used and a warning is raised.

**track_start.** `entry_date` from the book when it is a date; otherwise the first asOf the Risk Manager saw the position (track_source FIRST_RUN), with the initial stop set from that day's high-water mark. A SEED position with a known `entry_date` older than the available history starts at the first available row.

**Cooldown record.** When a STOP signal is written, a row goes into `state/cooldown.csv` with the trigger date and the trigger-day AdjClose, whether or not you sell. Because the signal repeats until you do, the cooldown does too: a name you did not sell is not an ADD candidate anyway.

## Ladder and exposure

**Index and peak.** twr_index (see "NAV and cash"). peakIndex is the highest value since baselineIndex was set. drawdown = (peakIndex - twr_index) / peakIndex, rounded to 6 decimals before any comparison, so a value such as 0.14999999999999997 does not fall on the wrong side of a 15% level.

**Rungs** (`ladder.levels`):

| Rung | Drawdown at or beyond | Max invested share of NAV |
|---|---|---|
| 0 | none | 100% |
| 1 | 10% | 75% |
| 2 | 15% | 50% |
| 3 | 20% | 25% |
| 4 | 25% | 0% (flat) |

**State machine, each run:**

1. rungFloor = the deepest rung whose drawdown level is reached.
2. rung = max(rung, rungFloor). The ladder steps down at once.
3. On the Friday rebalance run only, if rung > rungFloor and rung is below 4, and the conditions below hold, rung = rung - 1 (one step up per week). `lastReRiskWeek` stores the ISO week so a rerun cannot step twice.
4. Rung 4 sets `flatLocked`. While locked, rung stays 4 whatever the drawdown does.

**Re-risk conditions** (`ladder.reRisk`): the Analyst's active regime in `regime_history.csv` was BULL or TREND at the last 2 weekly rows, and twr_index is above its minimum of the previous 20 trading days.

**Manual restart.** Set `ladder.restartFrom` in `risk.json` to a date on or before today and after `flatLockedSince`. On the next run the lock is cleared, `baselineIndex` and `peakIndex` are set to the current twr_index, rung is set to 3 (25% invested), and `lastRestartFrom` records the date. The ladder then re-risks by the weekly rule. Without a restart the Risk Manager keeps asking for flat. Leave `restartFrom` as null otherwise.

**Final exposure cap** = min(regime cap, ladder cap), where the regime cap comes from `exposure.regimeCap[active_regime]` (100% for every regime as shipped, D9). The cap applies to the sum of tracked position values over NAV.

**Reduction when above the cap** (Monitor, any day). After other exits are decided, invested value V = sum of values of positions not already being sold. If V exceeds cap x NAV, sell in this order until the excess is gone:

1. Risk contribution of each position = value x (AdjClose - stop) / AdjClose / NAV, largest first.
2. Ties: SmallCap, then MidCap, then LargeCap, then ticker ascending.

Whole positions are signalled until the remaining excess is smaller than the next position. That position is trimmed to the cap if the trimmed notional is at least `sizing.minAdjustmentInr`; otherwise the trim is skipped and a CAP_NOT_REACHED warning records the residual. The reason is LADDER when the ladder cap is the lower of the two caps (also on a tie) and REGIME_CAP otherwise.

## Surveillance (NSE lists)

Python -m app.risk.surveillance downloads the daily lists from NSE and normalises them to `surveillance_{date}.json`:

```
{ "asOf": "2026-10-02", "fetchedAt": "2026-10-02T20:17:44+05:30", "sources": { "asm": "ok", "gsm": "ok", "t2t": "ok", "bands": "ok" },
  "asm": { "LT": { "XYZ": 2 }, "ST": { "ABC": 1 } }, "gsm": { "QRS": 3 },
  "t2t": ["LMN"], "bandPct": { "XYZ": 5, "ABC": 2 } }
```

**Sources are UNVERIFIED.** NSE's pages for the ASM list, GSM list, price bands and the daily price-band report exist, but no fetchable file location or column layout was confirmed in the research pass, and NSE blocks scripted requests that lack a browser-like session. `risk.json` ships with `surveillance.sources` set to `<set after probe>` and the stage refuses to run until the probe has filled them. The session handling (homepage first, cookies, realistic headers, 3 retries at 2, 4 and 8 seconds) reuses the helper in `app/metadata/data_source`.

**Rules the Monitor and Sizer apply** (D21):

| List | New entry (ADD, top-up) | Held name |
|---|---|---|
| ASM stage 1 or higher (long or short term) | Blocked, reason SURVEILLANCE | Warning in the digest only |
| GSM, any stage | Blocked | SELL signal, reason SURVEILLANCE |
| Trade-for-trade / BE series | Blocked | SELL signal, reason SURVEILLANCE |
| Price band at or below `surveillance.blockEntryBandPct` (5%) | Blocked | Warning only |

**A likely lower circuit** is a warning only: a held name with a known band whose daily return on asOf is at or below -(band - 0.1 percentage point) gets LOWER_CIRCUIT_LIKELY, because an exit signal for such a name may not be executable. No intraday data exists to confirm it.

**List missing or failed (D22).** Run uses the newest normalised list dated on or before asOf. For exit checks it is usable if it is at most `surveillance.staleExitDays` (3) trading days old. For entries it must be dated asOf, otherwise every BUY (ENTRY and TOPUP) is blocked with reason NO_SURVEILLANCE_DATA and the digest says so. Stops, ladder and exits never wait for the lists.

## Sizer

Runs only on a rebalance day with a valid targets file. It never runs on the Sunday fallback without targets.

### Inputs from the target file

Used: `rebalanceDate`, `regime` (raw, active, persistence), `composition`, and per bucket `strategy` (top_n, lookback, stock_trend_ma) and `selected` (ticker, rank, price, trendMa). **Not used:** `status`, `delta`, `limits`, `capital` and the cost fields. KEEP, ADD and DROP are recomputed here from `selected` and the book, because the shadow portfolio has different holdings from yours, and because the Analyst omits its delta when its holdings snapshot is older than 3 days.

### Delta against a book

| Result | Meaning |
|---|---|
| KEEP | Ticker selected now and held |
| ADD | Selected now, not held |
| DROP | Held and tracked, not selected. Reason: UNKNOWN_REGIME when the active regime is Unknown; NO_ALLOCATION when the ticker's bucket has composition weight 0, is missing from the file, or has top_n 0 for the active regime; otherwise NOT_SELECTED |

### Targets per selected name

For bucket b with composition weight w_b, and NAV from the cash ledger:

1. **Bucket budget** B_b = w_b x NAV x final exposure cap.
2. **Nominal width** s_i = clamp(k x ATR%_i, lo_b, hi_b) as a fraction of price (ATR% = ATR20 / AdjClose).
3. **Risk target** R = `sizing.riskPerPositionPct` x NAV (1.25%).
4. **Risk-based notional** N_i = R / s_i.
5. **Name cap** N_i = min(N_i, `sizing.nameCapPct[b]` x NAV) (Large 10%, Mid 8%, Small 6%).
6. If the sum of N_i in the bucket exceeds B_b, scale all of them down proportionally. If it is lower, the difference stays cash.

**Worked example.** NAV Rs 7,00,000; SmallCap; price Rs 250; ATR20 Rs 9 (3.6% of price). 3.5 x 3.6% = 12.6%, below the 18% lower clamp, so s = 18%. R = 1.25% x 7,00,000 = Rs 8,750. N = 8,750 / 0.18 = Rs 48,611, above the 6% name cap of Rs 42,000, so N = Rs 42,000. Quantity = floor(42,000 / 250) = **168 shares**, notional Rs 42,000, heat contribution 42,000 x 18% = Rs 7,560 (1.08% of NAV).

With the Analyst's current placeholders (SmallCap weight 1, BULL top_n 2), two such names use 12% of NAV and 88% stays in cash. That is the intended behaviour of a risk-based sizer, not an error.

### Quantity and funding order

Reference price is the raw `Close` of asOf. Quantity is `floor(notional / price)`. A name with quantity 0, or a notional below `sizing.minNewOrderInr` (Rs 25,000) for an ENTRY, is blocked with reason BELOW_MIN_NOTIONAL (ZERO_QTY when the quantity is 0).

BUY signals are funded in this order: buckets by composition weight descending, names by Analyst rank ascending. Each is limited by the lowest of:

- its target notional;
- the ADV cap: `liquidity.maxParticipationPct[b]` (1% Large and Mid, 0.5% Small) of the median of the last 20 days of raw `Close x Volume`; reason ADV_CAP. The cap applies to BUY only, never to a SELL, but a SELL larger than the cap raises a LIQUIDITY warning;
- available cash: ledger cash minus `sizing.cashBufferPct` x NAV (2%), plus, when `sizing.countSaleProceeds` is true, the estimated net proceeds of this file's SELLs (the broker's credit timing for sale proceeds is UNVERIFIED); reason INSUFFICIENT_CASH;
- the room under the final exposure cap; reason EXPOSURE_CAP;
- the room under the heat cap; reason HEAT_CAP (see below).

Whichever limit binds is recorded in the signal's `detail.limitedBy`. A limit that cuts the notional below the Rs 25,000 minimum blocks the ENTRY with that reason.

**Heat.** heat = sum over positions of value x (AdjClose - stop) / AdjClose / NAV. Existing positions use their current distance to the ratcheted stop; a new position uses its nominal width. `heat.capPct` is 12% of NAV (the low end of the 12 to 15% range, D26). Names are funded in order until the cap is reached; the next name is scaled to the remaining heat or blocked HEAT_CAP.

### Held-name resizing (KEEP)

A KEEP is resized only when both hold: |actual weight - target weight| is more than max(25% of target, `sizing.noTradeBand.absolutePct`, 2.5 percentage points), and the value of the trade is at least `sizing.minAdjustmentInr` (Rs 10,000). Overweight gives a TRIM (SELL, reason REBALANCE_TRIM), underweight a TOPUP (BUY, reason TOPUP), subject to the same limits as an ENTRY except that cooldown does not apply. Example: target weight 8%, band max(2%, 2.5 percentage points) = 2.5 percentage points; a position at 10.6% is trimmed, one at 10.4% is left.

### Deferral of rank-based DROPs (D27, D29)

A DROP with reason NOT_SELECTED is deferred, shown as HOLD_DEFERRED and not sold, when all hold:

- the book's `entry_date` is a date (never UNKNOWN);
- asOf is within `tax.deferral.windowDays` (28) days before entry_date plus 12 months (the anniversary);
- the unrealised gain on AdjClose against the book's average cost is at least `tax.deferral.minGainPct` (10%);
- AdjClose is above the trend MA for the position's bucket and the active regime, with the window taken from `buckets[b].strategy.stock_trend_ma` in the target file (no deferral if the bucket or window is unknown).

Deferred names are listed in `state/deferred_drops.csv` and re-checked on every Run. A deferred name is **released** (a SELL, reason DROP_DEFERRED_RELEASED, signalled when asOf reaches the anniversary so that execution on the next session falls after the 12-month mark) or **sold at once** with the triggering reason (STOP, LADDER, REGIME_CAP, SURVEILLANCE) when:

- asOf is on or after the anniversary;
- a STOP, ladder or regime-cap reduction, or surveillance exit applies;
- AdjClose closes at or below the trend MA;
- a new Friday list selects the ticker (it simply becomes a KEEP and leaves the deferred file).

NO_ALLOCATION and UNKNOWN_REGIME drops are never deferred. The deferral uses the book's single entry date. The Ledger keeps one average-cost lot per ticker, so a later top-up is not tracked as a separate lot (see "Known limitations").

### Cooldown (D8)

An ADD for a ticker is blocked with reason COOLDOWN until both hold: asOf is later than `release_after` in `state/cooldown.csv`, and the ticker's AdjClose on asOf is above `trigger_adj_close`. A DROP-based exit has no cooldown. After the first successful ADD signal following the release, the row is deleted.

### After a stop-out (D15)

Cash freed by a stop-out stays cash. The Sizer runs only on the weekly rebalance day, so no ADD is signalled between Fridays.

## Signal file

The one file you, or a later executor, read: app/data/risk/signals/signals_{asOf}.json. It contains no order types, no limit prices and no broker fields.

### Action list

| Field | Meaning |
|---|---|
| side | BUY or SELL |
| kind | ENTRY, TOPUP, TRIM or EXIT |
| qty | Whole shares, greater than 0 |
| refPriceInr | Raw Close on asOf |
| notionalInr | qty x refPriceInr |
| reason | See table below |
| detail | Rule trace: for example stopPrice, breachDate, lateBreach, drawdownPct, rung, limitedBy, alsoTriggered |
| estChargesInr | One-way charges at refPriceInr from the cost model |
| priority | 1 first. SELLs before BUYs; within SELLs STOP, SURVEILLANCE, LADDER or REGIME_CAP, DROP_*, REBALANCE_TRIM |

**Reason codes**

| Side | Reason | Rule that produced it |
|---|---|---|
| SELL | STOP | Close at or below the trailing stop |
| SELL | SURVEILLANCE | Held name entered GSM or trade-for-trade |
| SELL | LADDER, REGIME_CAP | Exposure above the ladder cap or the regime cap |
| SELL | DROP_NOT_SELECTED, DROP_NO_ALLOCATION, DROP_UNKNOWN_REGIME | Weekly delta |
| SELL | DROP_DEFERRED_RELEASED | A deferred DROP reached its anniversary |
| SELL | REBALANCE_TRIM | KEEP outside the no-trade band, overweight |
| BUY | ENTRY | New position from the weekly list |
| BUY | TOPUP | KEEP outside the no-trade band, underweight |

One SELL per ticker per run. When several conditions apply, the one with the highest priority is the reason and the rest are listed in `detail.alsoTriggered`. A ticker with a SELL cannot also have a BUY.

### Schema (schemaVersion 1)

```
{
  "schemaVersion": 1,
  "runId": "risk-2026-10-02T21:46:10+05:30",
  "generatedAt": "2026-10-02T21:46:18+05:30",
  "status": "ok",
  "asOf": "2026-10-02",
  "executionDate": "2026-10-05",
  "executionAt": "open",
  "weekly": { "included": true, "targetsFile": "targets_2026-10-02.json", "rebalanceDate": "2026-10-02" },
  "regime": { "raw": "TREND", "active": "BULL" },
  "nav": { "navInr": 706400.0, "cashInr": 412000.0, "positionsValueInr": 294400.0, "investedPct": 0.4168,
           "twrIndex": 1.0132, "peakIndex": 1.0132, "drawdownPct": 0.0 },
  "ladder": { "rung": 0, "maxInvestedPct": 1.0, "reRiskEligible": false, "flatLocked": false },
  "exposure": { "regimeCap": 1.0, "ladderCap": 1.0, "finalCap": 1.0, "heatPct": 0.047, "heatCapPct": 0.12 },
  "surveillance": { "status": "ok", "asOf": "2026-10-02" },
  "actions": [
    { "ticker": "XYZ", "bucket": "SmallCap", "side": "SELL", "kind": "EXIT", "qty": 40, "refPriceInr": 245.0,
      "notionalInr": 9800.0, "reason": "STOP", "estChargesInr": 24.0, "priority": 1,
      "detail": { "stopPrice": 246.0, "breachDate": "2026-10-02", "lateBreach": false, "alsoTriggered": [] } },
    { "ticker": "ABC", "bucket": "SmallCap", "side": "BUY", "kind": "ENTRY", "qty": 168, "refPriceInr": 250.0,
      "notionalInr": 42000.0, "reason": "ENTRY", "estChargesInr": 78.0, "priority": 3,
      "detail": { "rank": 1, "stopWidthPct": 0.18, "stopPriceAtEntry": 205.0, "limitedBy": "NAME_CAP" } }
  ],
  "holds": [ { "ticker": "QRS", "bucket": "MidCap", "action": "HOLD_DEFERRED", "release": "2026-10-20",
               "unrealisedGainPct": 0.21 } ],
  "blocked": [ { "ticker": "LMN", "intent": "ENTRY", "reason": "SURVEILLANCE" },
               { "ticker": "DEF", "intent": "ENTRY", "reason": "COOLDOWN" } ],
  "positions": [ { "ticker": "QRS", "bucket": "MidCap", "qty": 120, "avgCostInr": 410.0, "closeInr": 520.0,
                   "stopPrice": 441.0, "stopDistancePct": 0.152, "highWaterMark": 520.0, "trackStart": "2025-10-20" } ],
  "untracked": [ "ABCD" ],
  "warnings": [ "NAV_JUMP", "LOWER_CIRCUIT_LIKELY:XYZ" ]
}
```

The values above are illustrative. A rebalance day without targets writes `"weekly": { "included": false, "targetsFile": null }`.

**Consumer rules**

- Read `risk_status.json` first. If `app/data/risk/.lock` exists a run is in progress.
- A signal file is written once and never edited. A rerun for the same asOf is refused unless `--force`.
- Check `asOf` and `executionDate`. A file whose executionDate has passed is history, not an instruction.
- Files older than `signals.retentionWeeks` (104) are deleted at the end of each Run.
- A STOP for a ticker repeats until the book no longer holds it. A signal you cannot or do not execute does not disappear.

## Shadow portfolio (D11, D18)

The shadow book follows every signal exactly. It exists so you can see what the rules earn when followed, and how far your own trading drifts from them.

- **Start.** On the first successful Run (asOf recorded as `shadowStartDate`), the shadow copies your actual book and cash.
- **Each Run.** (1) Apply the previous run's shadow signals as fills at the raw `Open` of asOf (the execution day), BUY at Open x (1 + slippage bps), SELL at Open x (1 - slippage bps), with the Analyst's per-bucket slippage and charges. (2) Value at raw Close. (3) Run the same Monitor and Sizer functions on the shadow book, shadow cash and shadow state, producing shadow signals for the next session.
- **Cash shortfall.** A shadow BUY that exceeds shadow cash is reduced to the affordable whole shares, or skipped if none; a SHADOW_SHORTFALL warning records it.
- **No Open price** (halted or missing): the fill is retried the next trading day and a warning is raised.
- **Independence.** The shadow has its own cooldown, deferred drops and ladder. It uses the same targets, the same prices, the same surveillance lists and the same `risk.json`. It never reads your actual book after the start.
- **Adherence.** Each Friday the stats report counts shadow signals of the past week that match an actual fill (same ticker and side, within 3 trading days) over all signals, as `signalFollowedRate`.

## Evaluator (D28, D30)

Reporting only. A failed Evaluate never blocks Run. Evaluate reads state and signal files read-only and writes only its own files.

### Daily

Appends one row to `nav/nav_actual.csv` and `nav/nav_shadow.csv` (Run has already computed the actual row, so Evaluate adds the shadow row and the benchmark and regime columns, and verifies that both rows exist), and appends per-position rows to `nav/positions_daily.csv`.

### Weekly statistics (Friday), `reports/stats_{date}.json`

Windows: since inception, trailing 252 trading days, and each financial year (April to March) and calendar year. A window with fewer than `evaluator.minObs` (60) observations reports its figures with a LOW_SAMPLE flag. Computed for actual and shadow, with the benchmark ^NSEI alongside:

| Group | Metrics |
|---|---|
| Returns | Total return, CAGR (XIRR when flows exist, otherwise on twr_index), yearly returns by calendar year and by financial year |
| Risk | Annualised volatility (daily sigma x sqrt 252), maximum drawdown depth, drawdown duration (peak to recovery in days), ulcer index, 95% CVaR of daily returns |
| Risk-adjusted | Sharpe and Sortino using `evaluator.riskFreeRatePct`, Calmar |
| Versus benchmark | Beta, Jensen's alpha (annualised) from daily excess returns against ^NSEI, up and down capture. Notes that ^NSEI is a price index, so alpha is flattered by the dividends it omits (D12) |
| Bear statistics | Number of negative days for the strategy and for ^NSEI; **bear period** = days whose Analyst active regime was BEAR; compound return of the strategy and of ^NSEI over the bear period |
| Regime | Return, volatility and drawdown while each regime was active; **dominant regime** = the regime with the most days in the window |
| Per symbol | For each current and past holding: days held, maximum drawdown from its high-water mark since entry, return, and rank of contribution to P/L |
| Trades | Hit rate, payoff ratio, expectancy (p x average win minus (1 - p) x average loss) from `trading_journal.csv` net_pl, turnover (one-sided traded value over average NAV), cost drag |
| Rules | Slippage beyond stop (exit price versus the stop price, mean, 95th and 99th percentile, by bucket), whipsaw rate (stopped names re-selected within 4 weeks), time in market, signal adherence, tracking gap between actual and shadow |

Per-symbol and trade statistics come from the actual book only. Slippage beyond stop uses journal exits whose reason matches a past STOP signal for the ticker.

### Tax estimate (`reports/tax_{FY}.json`)

Per financial year, from `trading_journal.csv` rows with `source` FILLS or MANUAL_VERIFIED: realised short-term gains and losses (held up to 12 months), realised long-term gains and losses (more than 12 months), net of intra-year set-off (short-term losses against short and long-term gains, long-term losses against long-term gains only), estimated tax = `tax.rates.stcgPct` on net short-term gain plus `ltcgPct` on net long-term gain above `ltcgExemptionInr`, plus `cessPct` on the tax. Rows with entry_date UNKNOWN are excluded and listed. Also: realised net P/L after estimated tax and the same as a percentage of average NAV, and the list of open positions within 56 days of their 12-month mark with their unrealised gain.

Marked as an estimate, not tax advice. Surcharge, carry-forward of losses, indexation, any business-income treatment and the renumbered sections of the Income-tax Act 2025 are not modelled. The rates were read on 2 Oct 2026 (20% short-term, 12.5% long-term above Rs 1.25 lakh a year, 4% cess) and live in `risk.json` so they can change without a code change.

### Digest

One email per run through the same SMTP settings and credentials as the other systems. A failed or unconfigured send is logged and never fails the run.

| Stage | Digest contents |
|---|---|
| Surveillance | Per-source result, counts per list, failures, whether the normalised file was written |
| Run | Gate result, asOf, NAV, drawdown and rung, exposure caps, count of signals by reason, blocked entries with reasons, deferred drops, warnings, "no signals for {date}" or "exits only" when relevant |
| Evaluate | Row written, Friday statistics summary (return, drawdown, Sharpe, alpha, tracking gap), tax estimate summary, near-12-month list |

## Cross-cutting behaviour

**Concurrency.** All three stages take `app/data/risk/.lock` at start with an atomic create holding the PID and a timestamp. A stage that cannot get it exits 2 ("busy"). A lock older than 6 hours is stale and may be taken over. Run never takes the Analyst's or Ticker Data's lock; it only checks that `market/.lock` is absent. Every file write goes to a temp file and is atomically renamed.

**Idempotency.** Run is idempotent per asOf (see "Run gate"). Evaluate appends NAV rows keyed by date, so a rerun replaces the row for the same date rather than duplicating it. Surveillance is idempotent per date.

**Retries.** NSE downloads use the Metadata pattern: 3 retries at 2, 4 and 8 seconds with jitter. A retryable failure is not a data problem.

**Time.** All dates and cutoffs are IST (fixed UTC+5:30). Trading days come from `nse_calendar.json`.

**Logging.** Each stage logs to stdout and `app/data/logs/risk_{stage}_{date}.log`. Cookies and session values from NSE are never logged.

**Status file.** app/data/risk/risk_status.json holds one block per stage:

```
{
  "surveillance": { "status": "ok", "runDate": "2026-10-02", "finishedAt": "2026-10-02T20:19:02+05:30", "sources": { "asm": "ok", "gsm": "ok", "t2t": "ok", "bands": "ok" } },
  "run":          { "status": "ok", "runDate": "2026-10-02", "finishedAt": "2026-10-02T21:46:18+05:30", "asOf": "2026-10-02",
                    "lastGoodAsOf": "2026-10-02", "signals": 3, "weekly": true, "rung": 0, "signalsFile": "signals/signals_2026-10-02.json" },
  "evaluate":     { "status": "ok", "runDate": "2026-10-02", "finishedAt": "2026-10-02T22:31:40+05:30", "navRows": 1, "statsFile": "reports/stats_2026-10-02.json" }
}
```

**Housekeeping.** After each successful Run, copy `state/`, `nav/`, `shadow/` into `backup/{today}/`, delete backup folders older than 30 days, delete raw surveillance files older than 30 days, normalised ones older than 90 days, and signal files per `signals.retentionWeeks`.

## Configuration

app/config/risk.json holds every tunable. The values below are the ones you chose in the Q&A.

```
{
  "placeholders": false,
  "capital": { "cashFlowsFile": "app/config/cash_flows.csv", "reconcileField": null },
  "sizing": {
    "riskPerPositionPct": 0.0125,
    "nameCapPct": { "LargeCap": 0.10, "MidCap": 0.08, "SmallCap": 0.06 },
    "minNewOrderInr": 25000, "minAdjustmentInr": 10000, "cashBufferPct": 0.02,
    "noTradeBand": { "relative": 0.25, "absolutePct": 0.025 },
    "countSaleProceeds": true
  },
  "stops": {
    "atrPeriod": 20, "atrMultiplier": 3.5, "atrMethod": "sma", "priceBasis": "AdjClose",
    "clampPct": { "LargeCap": [0.10, 0.18], "MidCap": [0.14, 0.22], "SmallCap": [0.18, 0.28] },
    "bucketFallback": "SmallCap"
  },
  "cooldown": { "stopTradingDays": 10, "reentryAboveStopClose": true },
  "ladder": {
    "levels": [ { "drawdownPct": 0.10, "maxInvestedPct": 0.75 }, { "drawdownPct": 0.15, "maxInvestedPct": 0.50 },
                { "drawdownPct": 0.20, "maxInvestedPct": 0.25 }, { "drawdownPct": 0.25, "maxInvestedPct": 0.0 } ],
    "reRisk": { "regimes": ["BULL", "TREND"], "consecutiveWeeks": 2, "navAboveMinOfPreviousDays": 20 },
    "restartFrom": null
  },
  "exposure": { "regimeCap": { "BULL": 1.0, "TREND": 1.0, "WEAK": 1.0, "BEAR": 1.0, "Unknown": 1.0 } },
  "heat": { "capPct": 0.12 },
  "liquidity": { "advDays": 20, "maxParticipationPct": { "LargeCap": 0.01, "MidCap": 0.01, "SmallCap": 0.005 } },
  "surveillance": {
    "sources": { "asm": "<set after probe>", "gsm": "<set after probe>", "t2t": "<set after probe>", "bands": "<set after probe>" },
    "blockEntryBandPct": 5, "staleExitDays": 3, "exitOn": ["GSM", "T2T"],
    "maxRetries": 3, "backoffSeconds": [2, 4, 8], "runTime": "20:15", "retryUntil": "22:15"
  },
  "tax": {
    "deferral": { "enabled": true, "windowDays": 28, "minGainPct": 0.10, "requireAboveTrend": true },
    "rates": { "asOf": "2026-10-02", "stcgPct": 0.20, "ltcgPct": 0.125, "ltcgExemptionInr": 125000, "cessPct": 0.04 }
  },
  "gate": { "maxMissingShare": 0.10, "maxLedgerLagTradingDays": 0, "targetsWaitUntil": "Sun 22:00", "retryUntil": "08:00" },
  "shadow": { "enabled": true },
  "evaluator": { "benchmark": "^NSEI", "riskFreeRatePct": 0.055, "riskFreeAsOf": "2026-09-30", "minObs": 60, "statsDay": "Fri", "bearRegime": "BEAR" },
  "signals": { "runTime": "21:45", "retentionWeeks": 104 },
  "paths": {
    "risk": "app/data/risk", "market": "app/data/market", "analyst": "app/data/analyst", "upstreamStorage": "app/data/storage",
    "metadataConfig": "app/config/config.json", "analystConfig": "app/config/analyst.json",
    "calendar": "app/config/nse_calendar.json", "logs": "app/data/logs"
  },
  "lock": { "staleAfterHours": 6 },
  "mail": { "smtpHost": "<set at deployment>", "smtpPort": 587, "sender": "<set at deployment>", "recipients": ["<set at deployment>"] }
}
```

**Validation at load** (any failure exits 1, which --check also reports)

| Field | Rule |
|---|---|
| placeholders | Must be false for Run; Surveillance and Evaluate ignore it |
| sizing | Percentages in (0, 1]; minNewOrderInr and minAdjustmentInr greater than 0; nameCapPct has an entry for every bucket named in the Analyst's composition |
| stops | atrPeriod an integer of 2 or more; atrMultiplier greater than 0; each clamp has lo less than hi, both in (0, 1); one clamp per bucket; bucketFallback is a bucket name |
| ladder | levels sorted by drawdownPct ascending and maxInvestedPct descending, 1 to 6 levels, last level may be 0; restartFrom null or a date |
| exposure | Only regimes BULL, TREND, WEAK, BEAR, Unknown; each value in [0, 1] |
| heat, liquidity | Values in (0, 1] |
| surveillance | sources must not contain `<set after probe>` for the Surveillance stage; Run only needs the staleExitDays and exitOn fields |
| tax | Rates numbers of 0 or more; deferral.windowDays an integer of 1 or more |
| gate | maxMissingShare in [0, 1]; targetsWaitUntil a weekday and time |
| evaluator | benchmark is an index named in the market `indices.json`; riskFreeRatePct a number of 0 or more |
| cash_flows.csv | Exactly one OPENING row; dates not in the future; types as listed |

## Failure modes

| Failure | Behaviour |
|---|---|
| Ticker Data status not ready, `market/.lock` present, or no ^NSEI row for asOf | Run exits 3, hourly retry, failed-run digest "no signals for {asOf}" on the final attempt |
| Ledger did not succeed on asOf | Same as above |
| More than 10% of needed tickers lack a row for asOf | Same as above; Ticker Data's gap-fill closes it overnight |
| One held ticker has no row for asOf | Valued at last Close, no action for it, warning |
| Analyst targets missing on a rebalance day | Exit 3 until Sunday 22:00, then exits only (Monitor, no Sizer) with a digest note |
| Targets file has status other than ok or an unknown schemaVersion | Treated as missing |
| Holidays list empty for the run year | Run fails and alerts |
| risk.json invalid or `placeholders` true | Run fails, exit 1, digest |
| NSE lists unavailable on a day | Entries blocked (NO_SURVEILLANCE_DATA); exits use a list up to 3 trading days old |
| Surveillance sources not yet probed | Surveillance exits 1; Run sees no list and blocks entries |
| NAV jumps more than 15% with no flow | NAV_JUMP warning, run continues |
| No cash_flows.csv OPENING row | Run fails fast naming the file |
| ATR cannot be computed (fewer than 21 rows) | Upper clamp used, warning |
| Held ticker in no bucket file and never seen in one | bucketFallback used, warning |
| Stale lock | A lock older than 6 hours is taken over; a fresh lock gives exit 2 "busy" |
| Evaluate fails | Logged and digested; signals and state are unaffected |
| Email send fails | Logged; run result unaffected |
| Ladder file corrupt or missing | Run fails, no signals; restore from `backup/` or delete to restart from the current NAV (deletion resets the peak, so use `restartFrom`) |
| Disk loss | The 30-day backup lives on the same disk, so state, NAV history and the shadow book are lost. ⚠ Back up the host folder separately |

## Known limitations and risks

- **Signal only.** A signal that nobody executes does nothing. A stop-out signalled on Tuesday evening is only protective if the sell happens on Wednesday's open. The shadow portfolio shows the gap, the adherence figure counts it, and the digest repeats an unexecuted STOP daily.
- **End-of-day stops and gaps.** Stops are evaluated on the close and executed at the next open. A gap below the stop loses more than the stop distance, and a stock locked in its lower circuit cannot be sold at all. The Evaluator reports slippage beyond the stop so the cost is visible.
- **Stop parameters are not backtested.** The multiplier 3.5, the 20-day ATR and the clamps come from practice and one research pass, not from your data. The Analyst TDD keeps backtesting out of scope, and so does this one. Calibrate from the 150-ticker history before relying on them (see "Inputs needed before go-live").
- **Momentum crashes.** The research literature finds momentum crashes cluster in rebounds after market declines. A long-only book feels this as a sharp relative loss rather than a 70% month. The ladder re-risks one rung a week and only in BULL or TREND to reduce the chance of re-entering at the bottom.
- **NSE surveillance sources are UNVERIFIED.** File locations, formats and the ASM/GSM staging rules were not confirmed in the research pass. NSE blocks scripted access. The probe is the safeguard, and a failed download only blocks entries (D22).
- **One lot per ticker.** The Ledger keeps average cost and the first buy date. Tax deferral and the long-term/short-term split in the tax estimate treat a top-up as part of the first lot, which can misclassify it. The tax estimate is also simplified (see "Tax estimate").
- **UNKNOWN entry dates.** A position seeded or reconstructed without an entry date starts its stop history at the Risk Manager's first run, so any run-up before that is ignored and it is never tax-deferred.
- **Sale proceeds timing is UNVERIFIED.** The funding rule counts the estimated proceeds of the same file's SELLs. If Angel One credits only part of the proceeds immediately, a same-day BUY may be rejected at the broker.
- **Index and risk-free choices.** ^NSEI is a price index. The risk-free rate is a constant you maintain, not a fetched series.
- **Corporate actions.** Stops use AdjClose, so splits and bonuses do not cause false stops. Dividends make AdjClose differ slightly from the price you see. Timing differences between the Ledger (16:30) and the Updator rebuild (21:00) can produce a one-day NAV_JUMP.
- **Adjusted prices depend on Yahoo**, inherited from Ticker Data. The stop is replayed from history every run, so a restated AdjClose is picked up automatically, which can also move a stop.
- **Heat counts only price risk to the stop.** It does not include gap risk or correlation. There is no sector or correlation cap in v1 (D26).
- **The holiday calendar needs upkeep**, as in the other systems.
- **One account, one client code.** Nothing here supports several accounts.
- **A future executor has its own rules.** From 1 Apr 2026 Angel One accepts API orders only from a whitelisted static IPv4 address, treats every API order as an algo order, and prohibits market and IOC orders for algorithms, according to the Analyst TDD's research. Orders must be limit orders. That component is not designed here.

## Inputs needed before go-live

- Fill `app/config/cash_flows.csv` with the OPENING row (the cash balance of the tracked universe on the day the Ledger started) and any later deposits, withdrawals and dividends.
- Confirm `risk.json` values with a calibration run on your own history: the distribution of ATR20 as a percentage of price per bucket, so the multiplier 3.5 and the clamps match your universe. Re-check the heat cap, name caps and minimum order against your actual capital.
- Run `python -m app.risk.probe --check-nse`, then fill `surveillance.sources` with the confirmed locations and map the columns. Until that is done Surveillance will not run and Run will block entries.
- Set the risk-free rate (`evaluator.riskFreeRatePct`) and its date. The research pass saw about 5.5% for the 91-day T-bill in late September 2026; confirm it.
- Confirm the tax rates and the 12-month holding-period rule with your CA, including how a business-income treatment would change the estimate.
- Confirm the bind-mount path `C:\ProgramData\ticker-pipeline`, still flagged "confirm" in the other TDDs.
- Set the SMTP host, sender and recipients, and confirm the `--env-file` path is the same as the Analyst's.
- Fill the holidays and special sessions for the current year in `nse_calendar.json`.
- Decide how the host folder is backed up outside this system, including `app/data/risk/` and `cash_flows.csv`.
- Before relying on the Ledger book as a source, make sure the Ledger go-live checklist in the Analyst TDD is complete, including the probe and `seed_positions.csv`.

## Rules fixed while writing (not asked; change them if you disagree)

- The Risk Manager reads **no** `delta`, `status`, `limits`, `capital` or cost fields from the target file; it recomputes KEEP, ADD and DROP itself (needed for the shadow book and independent of the Analyst's 3-day holdings rule).
- Stops are replayed statelessly from `track_start` every run. ATR is a simple mean of 20 adjusted true ranges. The stop ratchets (never moves down).
- A STOP signal repeats every run until the book no longer holds the ticker. Every bar after `lastGoodAsOf` is checked.
- A STOP row in `cooldown.csv` is written when the signal is written, not when you sell.
- Sizing uses the nominal stop width, not the remaining distance to the stop.
- One SELL per ticker per run; reason priority STOP, SURVEILLANCE, LADDER or REGIME_CAP, DROP_*, REBALANCE_TRIM.
- BUY funding order: buckets by composition weight descending, names by rank ascending. Quantities are rounded down to whole shares.
- Heat cap 12% (the low end of the 12 to 15% range you accepted). The ADV cap applies to BUY only.
- Sale proceeds of the same file's SELLs count towards BUY funding (`countSaleProceeds`), flagged UNVERIFIED.
- Exposure caps apply to the sum of tracked position values over NAV. Deferred names count towards invested value but have no target.
- A bucket's weight, name caps and clamp come from the bucket name; a held ticker's bucket is the newest bucket file's, then the stored one, then the fallback.
- Ladder: step-down is immediate; step-up is one rung per Friday run and only when the conditions hold; rung 4 is a lock cleared only by `restartFrom`, which also resets the peak and starts at rung 3.
- Drawdown is on the time-weighted index, rounded to 6 decimals before comparison.
- NAV excludes untracked holdings. A NAV jump above 15% only warns.
- Held ticker with no asOf row: valued at last Close, no signal for it.
- The Sunday-after-deadline run proceeds without targets (Monitor only).
- The shadow starts as a copy of your book, uses the same `risk.json`, and never reads your actual book afterwards.
- Evaluator statistics treat BEAR as the bear regime for the bear-period figures; the dominant regime is the one with the most days.
- Risk-free rate is a config constant, not fetched.
- The cost model is read from `analyst.json` so there is one set of charge rates.
- Backups copy `state/`, `nav/` and `shadow/` after each successful Run and keep 30 days. Signals are kept 104 weeks.
- Stage code reuses the mailer, atomic-write, IST-time, calendar and cost helpers from app/market/ and app/analyst/, and the NSE session helper from app/metadata/.
- Exit codes follow the other stages: 0 ok, 1 failed, 2 busy, 3 gate not met.

## Changes needed in other components

- **Stock Analyst:** none for the Risk Manager to work. Add one line to its TDD and README that `limits` in the target file is informational and superseded by `risk.json` (D25). Optionally stop validating `limits` in `analyst.json` later; that is the Analyst's own decision.
- **Ticker Data and Metadata:** none. Both are read-only here.
- **Repo:** add `app/risk/` with modules `surveillance`, `run`, `evaluate`, `probe`, `sizer`, `monitor`, `stops`, `ladder`, `nav`, `shadow`, `evaluator`, `tax`, `surveil`, `common`; `Dockerfile.risk`; a row for `swing-trading-risk` in the CI image list and README table; `risk.json` and `cash_flows.csv` templates in `app/config/`.

## Test plan (for the implementation)

Unit tests with Yahoo, NSE and the broker always faked, as in the other systems:

- **Stop replay:** the Rs 246 example; ratchet never lowers; breach on a skipped day gives `lateBreach`; UNKNOWN entry starts at the first run; AdjClose across a split gives no false stop; fewer than 21 rows uses the upper clamp.
- **Ladder:** 0.14999999999999997 drawdown stays on the 10% rung; step-down immediate; step-up once per ISO week; rung 4 locks; `restartFrom` resets peak and starts at rung 3; deposit and withdrawal rows do not move the index.
- **Sizer:** the 168-share example; scaling to bucket budget; BELOW_MIN_NOTIONAL; heat cap scaling; ADV cap; funding order; Analyst placeholders (weight 1, top_n 2) leave about 88% cash; no-trade band (10.6% trimmed, 10.4% left).
- **Deltas and DROP reasons:** NOT_SELECTED, NO_ALLOCATION (weight 0, missing bucket, top_n 0), UNKNOWN_REGIME.
- **Deferral:** 15 days before anniversary, 10% gain, above trend defers; 9.9% gain, UNKNOWN date, below trend, NO_ALLOCATION do not; release signalled on the anniversary; STOP overrides deferral.
- **Cooldown:** blocked before release; blocked when below the trigger close; released after.
- **Surveillance:** each list's entry and exit rule; stale list for entries (blocked) and exits (used up to 3 days).
- **Gate:** each condition; Sunday fallback without targets; no signals on non-trading days; `--force` renames the old file.
- **Cash and NAV:** fills plus charges; OPENING row required; NAV_JUMP warning.
- **Shadow:** fills at next Open with slippage; shortfall; start copy; independent cooldown.
- **Evaluator:** bear-period compound return; drawdown duration; LOW_SAMPLE flag; tax set-off and the Rs 1.25 lakh exemption.
- **Config validation** table and `--check` for every stage.

## As-built decisions

Points the design left open or contradicted itself on, settled during the build (the ones marked * were approved in the build Q&A).

- *Run writes the complete actual and shadow NAV rows (all nine columns): it needs the shadow index for the shadow ladder and already has the benchmark, regime and rung. Evaluate only verifies both rows exist (a missing row is a failed evaluation, no back-fill), writes `positions_daily.csv`, the Friday statistics and the tax files. Evaluate is done for an asOf once its status block for that asOf is ok.
- *`surveillance.sources` is config-driven: each source is `{url, format: csv|json, symbolColumn, valueColumn?, termColumn?, filterColumn?, filterValues?, rowsKey?}`. `filterColumn` / `filterValues` pick, for example, the BE series from the trade-for-trade file; `termColumn` splits ASM into LT/ST. Surveillance and its `--check` refuse to run while any source is still `<set after probe>`. No NSE URL is hard-coded except the probe's UNVERIFIED candidate pages.
- *`paths.indices` (`app/config/indices.json`) was added to `risk.json` so `evaluator.benchmark` can be validated.
- `cash_flows.csv` is validated when Run starts (exactly one OPENING row, known types, no future dates), not by `--check`: the template ships header-only so the CI smoke-run passes. `OTHER` keeps its own sign (positive in, negative out). Only DEPOSIT, WITHDRAWAL and OTHER are external flows for the time-weighted index; OPENING and DIVIDEND are not. Flows are taken from every date after the previous NAV row, so a missed run does not lose a deposit.
- A missing `ladder_state.json` starts a fresh ladder at the current index (this is the documented "delete to restart"); a corrupt one fails the run.
- The shadow keeps its simulated fills in the Ledger's `fills.csv` layout (the actual book copied in as SEED / UNKNOWN rows) and its book is replayed from them, so a forced rerun of a day is exact. A fill whose Open was missing is retried for up to 7 days. The shadow has no external cash flows. Shadow warnings go to the digest, not into the actual signal file.
- The trend MA window for a deferral comes from the target file of the day, or the newest earlier one on days without targets. A deferred name with an unknown window is not deferred.
- The exposure-cap reduction runs in the Monitor, before the weekly DROPs are known, so on a Friday a name that is also dropped is listed in `alsoTriggered`. The SELL quantity of a ticker is the largest quantity any of its reasons asks for; the reason is the highest-priority one.
- A STOP repeats while the cooldown row written at the trigger is newer than the position's track start. Priorities in the signal file: STOP 1, SURVEILLANCE 2, LADDER / REGIME_CAP 3, DROP_* 4, REBALANCE_TRIM 5, BUY 6.
- Entries need today's list with all four sources ok. Exits use the newest list up to `staleExitDays` trading days old, whatever its source statuses.
- The cash limit on a BUY reserves the estimated charges. Heat for a top-up uses the nominal stop width.
- The ladder's weekly step-up is allowed on any rebalance day (also the Sunday fallback without targets).
- The final retry is the last attempt in the hour before 08:00 on the next morning (Monday after a Friday). Targets are awaited until `gate.targetsWaitUntil` of the asOf week.
- Statistics: trade and rule figures are computed once since inception; the windows (inception, trailing 252, each financial year, each calendar year) carry the return, risk, benchmark, bear and regime figures. Stats are written when asOf falls on `evaluator.statsDay`.
- Backups are dated by the run date.

## Sources

Internal inputs: your original Risk Manager notes, the Stock Analyst TDD (including "As-built decisions"), the Ticker Data System TDD, the NSE Ticker Metadata Pipeline TDD and the Swing-Trading-System README.

External facts come from a research pass dated 2 Oct 2026. Where only secondary or community sources were found, the item is marked UNVERIFIED above.

- Momentum risk: Barroso and Santa-Clara, "Momentum Has Its Moments" (Journal of Financial Economics, 2015); Daniel and Moskowitz, "Momentum Crashes" (JFE, 2016); Han, Zhou and Zhu, "Taming Momentum Crashes: A Simple Stop-Loss Strategy"; Alpha Architect's replication of the long-only winners leg.
- India momentum evidence: NSE Indices momentum strategy whitepaper (April 2026, data to 27 Feb 2026).
- Market structure: NSE price-band and circuit pages, NSE ASM and GSM frameworks (stage rules from NSE circulars and secondary summaries; NSE blocked automated fetches), NSE equity-market shortages and auction handling, market-wide circuit breaker rules, NSE pre-open session changes of 7 September 2026 (secondary sources).
- Algo framework: SEBI circular of 4 February 2025 and NSE implementation standards (NSE/INVG/67858 and the NSE FAQ of 3 November 2025); Angel One SmartAPI notices on the 1 April 2026 changes (static IP, market and IOC order prohibition, 9 orders per second).
- Costs and tax: Angel One brokerage and DP charge pages (via the Analyst TDD's cost model); Income Tax capital-gains rates for FY 2026-27 from secondary guides (20% short-term, 12.5% long-term, Rs 1.25 lakh exemption); RBI and CCIL 91-day T-bill yields, late September 2026.
- Classification: NSE Indices industry classification (for the Phase 2 sector cap, not used in v1).
