# Backtest Engine — Technical Design Document (as built)

Oct 3, 2026 · @Deba · Status: judge, tax overlay, walk-forward, overfitting gate, bhavcopy layer and point-in-time universe implemented; screener tier and HPO runner not built

This is the current description of the sibling `backtest/` package. It replaces the first TDD (`Backtest Engine.docx`) as the reference; `Backtest_Implementation_Plan.md` keeps the decision history, the measured runtimes and the real-data findings. Personal use. It never writes `app/` or `app/data/`, never places orders, and is not copied into any Docker image.

## Overview

The backtest replays the live Risk Manager over history. A day loop calls the app's own `app.risk.run.decide()` once per `^NSEI` trading day, so the rules that are tested are the rules that run live. Everything around that call is rebuilt for history: point-in-time data, weekly targets, next-open fills, dividends, tax and reporting.

| Piece | Module | Purpose |
|---|---|---|
| Data | `pit.py`, `prep.py`, `checks.py` | Read the Ticker Data store, as-of view, calendar and anomaly scan, Yahoo dividends |
| Targets | `targets.py` | Weekly regime and per-bucket picks from the app's own `selector.select_bucket` on panel slices |
| Judge | `replay.py`, `fills.py`, `dividends.py` | Day loop, shadow-exact fills, dividend credit, exits of vanished names |
| Overlay | `tax.py`, `surv_proxy.py`, `report.py` | FIFO tax and post-tax curve, surveillance proxy, metrics and stress windows |
| Search discipline | `params.py`, `walkforward.py`, `trials.py`, `overfit.py` | Parameter schema, folds and holdout, trial registry, overfitting gate (no optimiser) |
| Universe | `bhav.py`, `links.py`, `adjust.py`, `pituniverse.py`, `universe.py` | NSE bhavcopy, symbol links, split detection, point-in-time membership |
| Tools | `run.py`, `bench.py`, `workers.py`, `world.py`, `config.py` | CLI, runtime spike, one-thread workers, world builder, config validation |

## Principles

1. **One implementation of the rules.** Decisions come from `decide()`; the backtest patches only where state is read (`app.risk.run.load_state`, `app.risk.nav.read_nav`) so state lives in memory for the length of a run. A parity test runs the same window through the app's real `commit()` on a temp folder and requires identical NAV, fills and signals.
2. **No look-ahead.** Every read goes through an as-of view (`PitStore`); a canary test multiplies all data after day t by 37 and requires decisions up to t to stay identical. The one use of future knowledge is forcing the exit of a name whose series ended (see Vanished names).
3. **Shadow-exact fills.** Fill maths mirrors `app.risk.shadow.apply`, including its quirks (4-decimal price in the fill, 2-decimal cash, dedupe, 7-day retry). A parity test checks to the paisa.
4. **Everything labelled.** Reports state that surveillance is not modelled or provisional, that charges are today's rates, that the universe is survivorship-affected (mode `today`) or point-in-time with holes, and that tax is a draft until confirmed.
5. **Overfitting is guarded structurally.** Holdout reads are refused by the code, not by discipline.

## Configuration

`backtest/config/backtest.json` (validated by `config.py`; unknown or out-of-range values raise). The app's own configs are read as the base and edited in memory by `overrides.risk` and `overrides.analyst`.

| Section | Meaning |
|---|---|
| `window` | `start`, `end` (null = first known regime / end of data), `holdoutYears` 2 |
| `capital` | `inr` 100,000 and `composition` 50/30/20 (must equal the analyst composition) |
| `fill` | `mode: open`, `carryOverDays` 7, `realism` flags (bands, volumeCap, circuitLocks, settlementLag) which must stay false in v1 |
| `tax` | dated schedule (2008-04-01, 2018-04-01, 2024-07-23) and `confirmed` |
| `ladder` | `autoRestart`: `enabled` true, `afterSessions` 126 (see The judge) |
| `walkforward` | rolling 5y train / 1y test / 1y step, `purgeDays` 168 |
| `gate` | PBO <= 0.20, deflated Sharpe >= 0.95, OOS/IS >= 0.6, 80% of neighbours within 20% |
| `stress` | named windows (2008, 2013 and 2015-16, COVID, 2022 and 2024 election) |
| `surv` | `proxy` false; thresholds null until calibrated |
| `prep`, `bhav` | anomaly thresholds; NSE bhavcopy URLs, columns, series kept (EQ, BE, BZ), client retry and abort rules, cross-check tolerances (close 0.5%, volume 10%) |
| `universe` | `mode` (`today` or `pit`), `adjustValidated`, rank window 120 sessions (>= 60 observed), `scopeTop` 250, symbol map, `-RE` exclusion, NSE symbol-change layout, split-detector settings, `vanishHaircuts` [0.5, 1.0] |
| `compute` | `workers` (1 to 8), `seed` |
| `paths` | `backtest/data` (git-ignored), `app/config`, `app/data`, `params.json` |

`backtest/config/params.json` holds 43 tunable parameters (sizing 16, selector 15, stops 8, regime 4) with bounds, step and kind, and one cross-parameter constraint. It is validated against the live configs (every path must exist) and gated by `confirmed` (now true).

The only edits to `app/` are in the regime code: `regime.smaFast/smaSlow/momentumDays` (50/200/63) became configurable (`regime.windows_of`, validated in `analyst.common`) so they can be tuned; defaults reproduce the old behaviour exactly.

## Data

- **Prices:** the Ticker Data store, read-only. Stored `Close` is Yahoo's split-adjusted, price-only close (AdjClose/Close median 0.759 at series start), so cash dividends credited by the backtest do not double count. No raw price series exists, so splits are a non-event and fills use the stored `Open`.
- **Dividends:** `prep --dividends` fetches Yahoo dividends and splits into `backtest/data/dividends.csv` (the only network stage besides the bhavcopy stages). Credited as cash on the ex-date for the quantity held at the previous close; not an external flow, so TWR stays correct.
- **Calendar and anomalies:** `prep --scan` compares the app's trading calendar with `^NSEI` dates and scans for ZERO_VOLUME, DIV_STEP, DIV_UNLISTED, DIV_NO_STEP and BIG_MOVE. Known gaps: Yahoo omits Muhurat sessions and has no `^NSEI` rows for 2019-02-13 and 2019-03-29 (those days are not simulated); the app calendar lacks 2025-10-21.
- **Bhavcopy** (`bhav.py`): legacy `cm…bhav.csv.zip` to 2024-07-05 and UDiFF from 2024-07-08, resumable raw cache, one Parquet per year. A probe on your PC must pass before download. Early files have no ISIN; some use two-digit years; BE/BZ are kept (EQ preferred on a duplicate). `--crosscheck` compares Yahoo with NSE (PRICE_SPIKE, RATIO_BREAK, NO_BHAV_ROW, NO_YAHOO_ROW, VOLUME_MISMATCH); the Yahoo-versus-NSE close differs by a few percent in 2007 to 2009.

## Targets

`Targets` computes the regime history once (causal) and, on each weekly rebalance day, runs `selector.select_bucket` on slices of date-by-ticker panels for each bucket. It replaces `analyst.signals.build_targets`, which cannot run for a past date (bucket-file age check, own Store, registry on disk). A parity test compares the targets with `python -m app.analyst.signals --as-of` for every rebalance date of a synthetic world. The live data-quality gates and the registry's active flag are not reproduced. In point-in-time mode a bucket's columns change each week from the membership.

## The judge (day loop)

For each `^NSEI` trading day from the common start:

1. Credit dividends with ex-date today.
2. Close positions in names that have stopped trading (see below).
3. Fill queued signals at today's raw Open: bucket slippage, `costs.buy_charges/sell_charges`, cash cap, same dedupe as `shadow.apply`, retry for `carryOverDays` calendar days when there is no Open.
4. Build `Context` (as-of store, bucket membership of the day, `regime.live_rebalance_date`, windows from the newest targets, surveillance dict).
5. On a rebalance day build targets, otherwise none.
6. `decide(ctx, pf, None, bench_close, regimes, now, run_id)`; queue the actions; append the NAV row.

The common start is fixed from the slowest SMA the bounds allow (index row `smaSlow high + 9 + 5 x persistenceWeeks high`), so every trial covers the same window. The regime is Unknown during warm-up and the book sits in cash.

**Vanished names.** A held ticker whose series has ended is sold on the first session after its last row at that row's Close times (1 - write-off), with no charges (`fills.vanish`). The exit is a SELL fill, so tax and reports treat it as a sale. The write-off is 0 by default; the sensitivities are 50% and 100% (`universe.vanishHaircuts`). Series cut by an unresolved split break lose early history but do not end, so they are not vanishers. A merger leaves through the same rule rather than being linked as a rename.

**Flat-lock and the owner's restart.** The live ladder flat-locks at its last rung (25% drawdown here) and only unlocks when someone sets `ladder.restartFrom` in `risk.json`; a backtest has no such person, so one 25% drawdown in 2008 ended the first real runs (flat in cash for 14 years). With `ladder.autoRestart.enabled` the simulated owner restarts the ladder on the first day that is at least `afterSessions` sessions after the lock and on which the active regime has been in the ladder's `reRisk.regimes` (BULL/TREND) for `reRisk.consecutiveWeeks` weeks; the effect is the same as setting `restartFrom` to that day (`replay.simulate(restart_after=...)`, in-memory mode only). Reports list the restart days and say so in their labels; `--no-auto-restart` gives the live-faithful run. The rule is a modelling choice, not the live behaviour.

**Account size.** A Rs 1 lakh account cannot trade under the live `risk.json` (the sizer's smallest position is below `minNewOrderInr` 25,000). Sizing minimums, name caps and risk per position are therefore tunable parameters; a plain `--single` or `--compare` run at the live config makes no trades (the CLI warns). Pass values from `params.json` with `--set`, e.g. `--set sizing.minNewOrderInr=3000 --set sizing.minAdjustmentInr=1500`. Report labels follow `universe.mode`.

## Tax overlay

Decisions use the pre-tax NAV, as live. Afterwards `tax.py` matches the fills FIFO per ticker, applies the dated schedule by sale date (short-term losses offset long-term gains, the long-term exemption applies once per year) and subtracts tax from the curve on the last simulated day of each financial year. Not modelled: surcharge, loss carry-forward, 31-Jan-2018 grandfathering, dividend income tax. Reports label the schedule a draft unless `tax.confirmed` is true. Decisions keep the live book's first-buy entry date; the report counts how often that differs from the FIFO view.

## Surveillance proxy

No surveillance history is stored, so ASM-like (repeated circuit-like days) and T2T-like (thin trading) flags are inferred from prices; GSM is not proxied. Thresholds ship null and the proxy stays off until `--calibrate` against the retained `surveillance_*.json` files fills them; reports then say PROVISIONAL.

## Search discipline

- **Windows:** the holdout (last 2 years) is carved out first. Rolling 5y/1y/1y folds decide; anchored folds cross-check. The purge is in trading days: the larger of `purgeDays` and the longest look-back any allowed parameter can use (250 with the current bounds). A tuning run asking for holdout data raises `HoldoutRead`, and the holdout is scored once for one parameter set (a marker file refuses a second set).
- **Registry:** `trials.Registry` appends a JSON line per evaluation (trial id, config hash, parameters, window, metrics, seed, code sha, data hash) and stores post-tax daily returns per trial. `Session.evaluate`, `score_holdout`, `gate_report` are the interface an optimiser would use; no optimiser is built.
- **Gate:** probability of backtest overfitting by CSCV, deflated Sharpe (corrected for the number of distinct parameter sets), OOS/IS retention and neighbourhood stability, with the limits in `gate`. `gate_report` refuses while `params.json` is unconfirmed.
- **Objectives:** post-tax CAGR (max), max drawdown (min), ulcer index (min), all on the post-tax curve.

## Point-in-time universe

Today's 143 names are a biased sample: only 47 of 2008's 150 most-traded names are in it (2013: 58, 2018: 68, 2023: 81). Mode `pit` removes most of that bias.

1. **Links** (`links.py`): renames joined by ISIN chain, NSE's headerless symbol-change file (fields counted from the end) and a manual `symbol_map.csv`. Mergers are not linked.
2. **Prices** (`adjust.py`): for names Yahoo does not carry, the bhavcopy is adjusted by a split detector. A move over 30% is a split/bonus when the factor is a usual one (1.5, 5/3, 1.75, 2, 2.5, 3, 4, 5, 6, 8, 10, 20, 50, 100) within `niceTolerance` AND either the volume shifts by that factor (within `volumeTolerance`) or the price is within `tightTolerance` and the day does not look like a crash. Unchanged volume means a genuine move and is kept. Anything unresolved cuts the series after the last break and is listed. Tuned against Yahoo's split list (`--tune-adjust`): 90% recall (146 of 163) with 11 false events at 0.10 / 0.10.
3. **Membership** (`pituniverse.py`): on every weekly rebalance date, names are ranked by median traded value over the last 120 sessions (at least 60 observed), `-RE` entitlements excluded; the top 50 / next 50 / next 50 become Large / Mid / Small. Scope is names in the top 250 at any month end (873 names: 133 from Yahoo, 740 from the bhavcopy). A name with no usable series is skipped and counted as a "hole".
4. **Residual bias** (`--build-pit`): 14 unresolved breaks; holes mean 1.0 of 150 slots per date (max 4; 2008 about 2.2, 2017 about 2.4, 2020 onward near 0). Bhavcopy-derived names carry no dividends.
5. **Engine:** `PitData.members(asof)`, `Targets` and `replay.simulate` use the membership. With a membership equal to the static buckets, NAV and fills are identical to `today` mode (test). Mode `pit` is refused until `universe.adjustValidated` is true.

## CLI

Run from the repo root, `--check` first, exit codes 0 ok, 1 failed, 2 busy, 3 gate not met (run the upstream stage first).

| Command | Purpose |
|---|---|
| `backtest.run --check` | validate config and the app side; no network, no writes |
| `backtest.run --single [--start --end --set K=V]` | one judge run; writes `backtest/data/runs/run_*.json` |
| `backtest.run --compare [--start --end --set K=V --workers N --no-auto-restart]` | today's names vs point-in-time at 0% / 50% / 100% write-off; prints post-tax CAGR, max drawdown, Sharpe, exits, rupees written off; one report per case. The four cases run in parallel processes (default `compute.workers`, one native thread each); each prints a progress line per 5% with the simulated date, elapsed time and ETA. `--single` prints the same |
| `backtest.prep --check`, `--dividends`, `--scan` | data readiness, Yahoo dividends and splits, calendar and anomaly report |
| `backtest.surv_proxy --snapshot`, `--calibrate` | keep the app's surveillance lists; score proxy thresholds |
| `backtest.bench [--years --profile --workers --scaling --set K=V]` | runtime spike |
| `backtest.bhav --check`, `--probe`, `--download`, `--build`, `--crosscheck [--strict]`, `--summary`, `--universe-stats` | bhavcopy layer |
| `backtest.universe --links`, `--probe-symbolchange`, `--validate-adjust`, `--tune-adjust`, `--build-pit` | point-in-time universe |

## Performance

Measured on your 12-thread PC: 104 ms per session with trades (about 6 minutes per 14.8-year run); about 85% of the time is inside `decide()` (the stop engine replays each open position daily). Process pools run one native thread per worker (`workers.py`): 5.1x throughput at 8 workers against 2.2x with default thread pools. A 30-point strict gate (130 simulations) is projected at about 2.6 h on 8 workers. A faster screener tier was deferred pending the trial budget.

## Tests

Offline and synthetic; no network. `tests/app` (root project, `uv run pytest`, coverage of `app`) and `tests/backtest` (`uv run --project backtest pytest -c backtest/pyproject.toml`).

| Area | Test |
|---|---|
| Regime edit | existing regime tests green, defaults identical |
| Targets | parity with `signals --as-of` |
| Decision | in-memory run equals run through `commit()` |
| Fills, costs | parity with `shadow.apply`; Rs 293.28 round trip |
| No look-ahead | poison-after-t canary |
| Accounting | cash + holdings = NAV, no negatives, lots reconcile to fills |
| Determinism | same config and data hash gives identical output |
| Gate | known-overfit synthetic strategy fails, robust one passes; holdout reads refused |
| Universe | split detector, links, membership labels independent of later data, pit equals static buckets |
| Vanished names | exit date and price, exact write-off, nothing changes before the exit |
| Security | file loaders go through the app's `safe_path` (paths must stay inside the working directory) |

Needs your PC (no `app/data/` in the cloud): golden file for a fixed 2-year run, calendar check against real holidays, real-date targets parity.

## Known limits and open items

- Surveillance is not modelled until the proxy is calibrated; realism flags (bands, volume cap, circuit locks, settlement lag) are off and unimplemented.
- Charges use today's rates for all years; benchmark is the price index.
- The tax table must be verified by you before `tax.confirmed` is trusted (set true in the shipped config).
- Remaining universe bias: holes in the top 150, names with unresolved splits, no dividends on derived names, merger consideration modelled only as exit at the last price.
- Not built: screener tier, optimiser/HPO runner, bhavcopy gap fill of missing Yahoo days, golden-file test.

## First full-history results (2008-07-25 to 2026-10-01, Rs 1 lakh, sizing minimums 3,000 / 1,500, owner restart after 126 sessions)

Recorded so the next reader knows what the engine produced before any tuning. Tax schedule confirmed, charges at current rates, surveillance not modelled. Not a recommendation: the parameters are the live defaults with only the two sizing minimums lowered so a Rs 1 lakh account can trade.

| | Today's names | Point-in-time |
|---|---|---|
| Post-tax CAGR | -0.8% | -3.7% |
| Max drawdown (post-tax) | -38.3% | -53.3% |
| Sharpe | -0.69 | -1.39 |
| Fills / turnover | 2,350 / 70.7x | 1,182 / 38.6x |
| Time in market | 86% | 67% |
| Charges / average NAV (whole run) | 0.72 | 0.45 |
| Ladder restarts | 2012-11-26 | 2010-10-21, 2019-04-12 |
| Names later stopped / write-off exits | 0 / 0 | 0 / 0 (write-off 50% and 100% identical to 0%) |

Benchmark (Nifty price index): +9.8% CAGR, max drawdown -45.4%, Sharpe 0.29. Readings: (1) the live rules at these settings do not beat the index over this window; (2) the point-in-time universe costs about 3 points of CAGR against today's names, an estimate of the survivorship bias in a losing strategy, with the residual-bias caveats above; (3) charges are the main leak at this account size (about Rs 27 per fill against positions of a few thousand rupees), which the sizing minimums should be tuned against; (4) the 2008 start (a 21% realised loss in the first FY) triggers the flat-lock in every run.
