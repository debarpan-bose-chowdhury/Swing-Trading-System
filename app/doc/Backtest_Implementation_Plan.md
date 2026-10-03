# Backtest Engine: implementation plan (v1)

Companion to `Backtest Engine.docx` (the TDD). The TDD stays the requirements source; where this plan differs, the difference is listed in section 1 and was decided with you. Written 2026-10-03 from a read of `app/risk/*`, `app/analyst/*`, `app/market/*` and `app/config/*`.

## 1. Decisions that change the TDD

| # | Area | TDD said | Plan (your answer) | Why |
|---|---|---|---|---|
| D1 | Prices | Raw prices plus a split/bonus factor table | Use the stored Yahoo series as-is. Dividends come from Yahoo (`Dividends` column) | Stored `Close` is already split-adjusted (`fetcher.py`: `auto_adjust=False`), so raw prices do not exist and splits are a non-event |
| D2 | State I/O | RAM-backed sandbox folder per trial | Patch two seams in memory: `app.risk.run.load_state` and `app.risk.nav.read_nav` | No Windows RAM disk, no folder collisions, no quadratic NAV-CSV rewrite. Guarded by a decision-parity test against a temp-folder run |
| D3 | Regime params | Tune SMA 50/200 and the 63-day return | One small `app/` edit: `regime.smaFast/smaSlow/momentumDays` in `analyst.json` (50/200/63), warm-up `smaSlow + 9` | `raw_regimes` hard-codes them. This reverses the TDD's "zero edits to app/" for this one file only |
| D4 | Fills | Realism layers part of the exact tier | Default = `shadow.apply` semantics. Bands, volume cap, circuit locks and settlement lag are OFF-by-default flags reported as sensitivity | Keeps results equal to the live shadow portfolio and parity exact |
| D5 | Tax | Consume `risk.tax.estimate` | Overlay on the pre-tax NAV: decisions use pre-tax NAV (as live); the post-tax curve subtracts tax at each FY end. FIFO lots from the fill stream, dated statutory table | `estimate` applies one rate set to all years and its journal is average-cost |
| D6 | Charges | Dated Angel One schedules | Today's `analyst.json` costs for all years, labelled "current-rate charges". Schedule stored as a list so history is a data-only addition later | Historic brokerage/STT/stamp are not in the repo |
| D7 | Surveillance | Proxy with open thresholds | Proxy calibrated on the retained `surveillance_*.json` (at most 90 days), snapshotted forward. Thresholds PROVISIONAL | The app purges older lists |
| D8 | Screener | Tier 1 vectorbt, built 7th | Deferred. Decide after a runtime spike on your PC. NautilusTrader dropped unless a spike asks for it | Second implementation to maintain and calibrate |
| D9 | Window | 2008-01-01 | Common fixed start from the slowest SMA allowed in the params bounds. Holdout = last 2 years | Tuning the slow SMA otherwise gives each trial a different window |
| D10 | Folds | train/test lengths unspecified | Rolling 5y train / 1y test / 1y step; anchored cross-check expanding train, 1y test; 168-day purge and embargo | Roughly 10 folds over 2008 to Oct 2024 |
| D11 | Gate | Limits open | PBO <= 0.20, deflated Sharpe >= 0.95, OOS/IS >= 0.6, neighbourhood: >= 80% of adjacent grid points within 20% on every objective | Approved by you |
| D12 | Fill flags | Volume cap and carry-over open | Cap = `liquidity.maxParticipationPct` of the fill day's traded value; carry-over = `shadow.LOOKBACK_DAYS` (7 calendar days) | Reuses existing numbers, none invented |
| D13 | Cross-check | Tolerances open | Check anomalies (|return| > 25%, dividend steps, zero volume) plus a random 2% of ticker-days against bhavcopy. Close mismatch > 0.5% = flag (strict mode aborts); volume mismatch > 10% = report only | Approved by you |

Defaults I took from the code (reverse any of them):
- **DP charge:** per sell fill (per scrip per day), as `costs.sell_charges` and `shadow.apply` do. The TDD's "once per sell day" is read as loose wording. A single round trip is still Rs 293.28.
- **Entry date:** decisions (deferral rule) keep the live book's first-buy entry date; tax uses true FIFO lots. The report states how often the two views disagree.
- **Trial registry:** append-only JSON lines (stdlib, atomic append), not Parquet, which cannot be appended cheaply.
- **Tests:** `backtest/tests`, run with `uv run --project backtest pytest backtest/tests`. The root `pytest` (coverage of `app`) is untouched.
- **Stress windows (config, editable):** 2008 crisis 2008-09-01..2009-03-31, 2013 taper 2013-05-22..2013-09-30 plus 2015-08-01..2016-02-29, COVID 2020-02-01..2020-12-31, 2022 calendar year plus 2024-06-03..2024-06-10.
- **Objectives:** post-tax CAGR (max), max drawdown (min) and ulcer index (min), all on the post-tax curve.

## 2. Findings from the code review (what the plan has to respect)

- `decide()` is pure apart from `load_state`, `nav.read_nav`, `ctx.hist` (reads `ctx.store`) and `datetime.now` in the signal body. It needs `ctx.targets`, `ctx.rebalance`, `ctx.windows`, `ctx.surv` and a `Portfolio` with a book DataFrame.
- `ladder.step` receives the weekly active-regime history up to `asof`. `regime_history` is causal, so it is computed once and sliced per day.
- Regime is Unknown for the first `smaSlow + 9` index rows; Unknown selects nothing, so the portfolio sits in cash until then.
- `shadow.apply` reads every shadow signal file each day, so calling it for about 4,500 days is quadratic in I/O; `fills.py` mirrors it instead.
- `signals.build_targets` cannot run historically: `bucket_universe` has a 7-day file-age check, `Store` is built inside, and the registry is read from disk. `targets.py` calls `selector.select_bucket` directly.
- A STOP is regenerated by `decide` every day until the position is gone. A missed BUY is retried only by shadow's 7-day lookback.
- Live shadow never credits dividends (`flows=None`). The backtest credits them as cash; they are not an external flow, so TWR stays correct.
- Versions checked on PyPI: vectorbt 1.1.1 exists. This mirror shows NautilusTrader 1.221.0 as the newest, not the TDD's 1.231.0 (irrelevant while Nautilus is dropped).

## 3. Layout

```
backtest/
  pyproject.toml        # depends on the repo root (path) + pandas, pyarrow, numpy; yfinance only for prep
  config/backtest.json  # placeholders:false only after every user-set value is recorded
  data/                 # git-ignored: dividend cache, surveillance snapshots, trial registry, golden files
  prep.py               # only network stage: dividends (+splits for the anomaly check), bhavcopy sample; --check offline
  pit.py                # read-only loader, Store-compatible as-of view, wide AdjClose/value panels built once
  targets.py            # regime slice + select_bucket on panel slices -> targets dict
  replay.py             # day loop, seam patches, Context per day, calls decide()
  fills.py              # shadow-exact fills, incremental average-cost book, optional realism flags
  dividends.py          # ex-date cash credit
  tax.py                # FIFO lots, dated statutory table, FY-end post-tax curve
  surv_proxy.py         # price-behaviour proxy + calibration command
  report.py             # metrics, stress windows, benchmark, labels ("upper bound, survivorship-biased")
  params.py walkforward.py trials.py overfit.py   # HPO-ready interfaces, no runner
  run.py                # CLI: single run, folds, stress; --check; exit codes 0/1/2/3
  tests/
```

Repo conventions kept: run from the repo root, `--check` first (no network, no writes), exit codes 0 ok, 1 failed, 2 busy, 3 gate not met, `placeholders` gate, nothing printed from `.env`, no orders. `backtest/data/` is added to `.gitignore`. Existing Dockerfiles copy only `app`, so nothing reaches an image.

## 4. The day loop (judge)

For simulated day t (every `^NSEI` trading day from the common start):
1. Fills: signals from the previous run whose execution date is t (and retries inside the 7-day window) fill at the raw Open of t: bucket slippage, `costs.buy_charges/sell_charges`, cash cap, same dedupe as `shadow.apply`.
2. Dividends with ex-date t credit cash for quantity held at the previous close.
3. Build `Context` for t (as-of store view, `buckets_of`, `rebalance` flag from `regime.live_rebalance_date`, `windows` from the newest targets, proxy `surv`).
4. On a rebalance day, build targets from the panel slice; otherwise `ctx.targets = None`.
5. `decide(ctx, pf, None, bench_close, (regime_now, regime_history), now, run_id)` against the in-memory state and NAV.
6. Queue the actions; store the returned state; append the NAV row.

After the loop, `tax.py` turns the fill stream into FIFO lots, charges tax at each FY end and emits the post-tax curve; `report.py` computes the objectives and stress windows.

## 5. Tests (pass before any result is quoted)

| Test | Runs here (synthetic) | Needs your PC (real data) |
|---|---|---|
| Regime edit leaves existing regime tests green and defaults identical | yes | no |
| Targets parity vs `python -m app.analyst.signals --as-of` | synthetic store | sampled real dates |
| Decision parity (in-memory seams vs temp-folder `commit`) on a frozen short window | yes | optional |
| Fill parity vs `shadow.apply` on the same orders, to the paisa | yes | no |
| Cost parity: Rs 293.28 round trip | yes | no |
| Look-ahead canary: poison data after t, decisions at t unchanged | yes | yes |
| Accounting invariants (cash + holdings = NAV, no negatives, FIFO lots reconcile to fills) | yes | yes |
| Determinism: same seed, config, data hash gives byte-identical output | yes | yes |
| Corporate actions: NAV continuous across a known split/bonus date | synthetic | listed real dates |
| Calendar: every simulated date is a trading day; special sessions present; holiday file vs `^NSEI` dates 2008 to 2023 | no | yes |
| Gate: known-overfit synthetic strategy fails, known-robust passes | yes | no |
| Golden file for a fixed 2-year run | no | yes |

## 6. Phases and exit criteria

| Phase | Work | Done when |
|---|---|---|
| 0 | `regime.py` edit + `analyst.json` keys + regime tests; `backtest/` skeleton, `pyproject`, `.gitignore`, `--check` | root `uv run pytest` green; `python -m backtest.run --check` ok |
| 1 | `pit.py`, calendar/holiday check, `prep.py` (dividends, anomaly scan) | data report lists gaps, anomalies and dividend steps |
| 2 | `targets.py` | targets parity passes on sampled dates |
| 3 | `fills.py`, `replay.py`, seam patches | fill, cost, decision parity, canary, invariants, determinism pass |
| 4 | `dividends.py`, `tax.py`, `surv_proxy.py` (+ calibration), `report.py` | post-tax curve, stress windows, benchmark; proxy on/off sensitivity |
| 5 | `params.py`, `walkforward.py`, `trials.py`, `overfit.py` | synthetic gate test passes; splitter refuses holdout reads |
| 6 | Runtime spike on your PC (18 years, 148 names, 8 procs) | measured seconds per run and per fold decide whether a screener is built |
| 7 | Phase 2 survivorship | separate TDD |

## 7. Still needs you (none blocks phases 0 to 3)

- Confirm the tax table: 2008-04-01 STCG 15%, LTCG exempt, cess 3%; 2018-04-01 LTCG 10% over Rs 1 lakh, cess 4%; 2024-07-23 STCG 20%, LTCG 12.5% over Rs 1.25 lakh.
- Run the data-dependent tests and the runtime spike on the Windows PC (no `app/data/` in the cloud container); share the timings.
- Total-return benchmark source (deferred; the price index is used until then).
- Trial budget per HPO campaign (decides the screener).
- Confirm or edit the stress-window dates and objective definitions in section 1.

## 8. Progress and deviations (updated after Phase 5)

Done and pushed: phases 0 to 5 (see `git log`). Offline tests: `uv run --project backtest pytest backtest/tests`.

Deviations from sections 1 to 7, all decided with you or forced by the code:
- **Account size:** a Rs 1 lakh account never trades under the live `risk.json` (smallest position the sizer can open is below `minNewOrderInr` 25,000). You chose to keep Rs 1 lakh and tune the sizing minimums, name caps and risk per position (`params.json`, group "sizing"). Until a value is confirmed a plain `--single` run at the live config makes no trades.
- **Purge unit:** `walkforward.purgeDays` is read in trading days (the selector's look-backs are). The effective purge is the larger of the config value and the longest look-back any allowed parameter can use: 250 sessions with the proposed bounds (slow SMA up to 250).
- **Common start:** index row `smaSlow high + 9 + 5 x persistenceWeeks high` (about early 2009 with the proposed bounds).
- **Parameter bounds** in `backtest/config/params.json` are PROPOSED around the live defaults (`confirmed: false`); `Session.gate_report` refuses to run until you review them and set `confirmed` to true. `minNewOrderInr` and `minAdjustmentInr` live values (25,000 / 10,000) lie outside the proposed bounds, so their default point is clipped to the bound.
- **Charges, dividends tax, bhavcopy cross-check** are unchanged from section 1; the bhavcopy parsers are still to be built before the golden file.

### Runtime spike (Phase 6): first numbers, synthetic world at real scale

`python -m backtest.bench` times the judge on whatever is in `app/data`. These figures come from a synthetic 148-name, 4,957-row world on a 4-core cloud box, NOT your data or PC; re-run it there (`--workers 8`) before deciding on the screener.

| Measure | Result |
|---|---|
| Build (load prices, regime, panels) | 2.7 s, about 350 MB per process |
| One run over the tuning region (15 years, 3,896 sessions) | 286 s = 73 ms per session = 18.5 s per simulated year |
| 4 runs in parallel processes | 1.00x per worker, throughput 4.0x (no contention) |
| Strict gate, 30 tried points (134 simulations, 1,791 simulated years) | 9.2 h on one worker, about 1.1 h on 8 workers at ideal scaling |

Where the time goes: about 85% is inside the app's own `decide()`, mostly `monitor.holdings` replaying `stops.stop_path` from each position's track start every day (about 7 open positions per day). Arrow-backed Date strings are not the cause (object dtype saved 4%). Cutting it needs either an incremental stop path (not bit-identical to the app's rolling mean, so the exact-parity tests would need a tolerance) or the screener tier; neither is built.

### Phase 7a: bhavcopy layer (built); universe work waits for real counts

Decided with you: bhavcopy layer first, probe-first formats, bucket rule and dead-name handling decided after the data is in (dead-name rule when it comes: detect breaks, adjust only what a corporate-action file confirms, exclude the rest and list every exclusion).

`python -m backtest.bhav`: `--probe` (one file per format, checks the configured URL and column names; `--download` refuses until both pass), `--download` (resumable raw cache under `backtest/data/bhav/raw/`, 404s remembered), `--build` (one Parquet per year), `--crosscheck [--strict]` (Yahoo vs bhavcopy over every common date: PRICE_SPIKE, RATIO_BREAK = split/bonus step, NO_BHAV_ROW, NO_YAHOO_ROW, VOLUME_MISMATCH report-only). With the whole history cached the check covers every ticker-day, a superset of the approved "anomalies plus 2% sample". Close tolerance 0.5%, volume 10%, as approved. The NSE hosts are not reachable from the cloud, so the URL templates and column names in `backtest.json` stay unverified until `--probe` passes on your PC.

Not built yet (Phase 7b, needs the real counts): point-in-time universe, bucket rule, dead-name corporate-action handling, and feeding dead names' raw prices into `pit.py`.

### Runtime spike on your PC (first real run, `bench --workers 8`)

Real data: 143 names (all with history), 4,672 index rows, common start 2008-11-19, tuning region to 2023-09-21 (3,637 sessions). Build (load, regime, panels) 17 s.

| | Cloud box, synthetic, NO trades | Your PC, real data, NO trades |
|---|---|---|
| ms per session | 19 | 62.5 (15.8 s per simulated year, 227 s per 14.8-year run) |
| 8 parallel runs | n/a (4 cores) | slowest worker 1.41x slower than alone, throughput 5.7x, 16 s to start workers |

That run made 0 fills (Rs 1 lakh cannot meet the live Rs 25,000 minimum order), so it measures targets, the ladder and bookkeeping only. A trading run costs more: on the cloud box trades took a run from 19 to 73 ms per session (3.8x), mostly the stop engine replaying each open position every day. Scaling that to your PC gives an ESTIMATE of about 240 ms per session, 14 minutes per 15-year run, and a 30-point strict gate of about 29 h on one worker or about 5 h on 8 workers. Re-run with `--set sizing.minNewOrderInr=3000 --set sizing.minAdjustmentInr=1500` for the real figure.

### Phase 7a on your PC: first real result and fix

`--probe` passed for the 2015 legacy and 2024 UDiFF samples (real headers match the configured names). `--download` then failed on every 2007 day: the early legacy layout has no `ISIN` column (and a trailing comma). Fixed: `ISIN` is optional, the probe now checks 4 legacy days (2007-09-17, 2008-01-02, 2015-06-02, 2024-07-05) and 2 UDiFF days, and `--download` stops after 10 consecutive failures (`bhav.client.abortAfterFailures`) instead of walking all 4,600 days. Unreadable files are never cached, so nothing needs cleaning up. Other layout drift between 2008 and 2024 is still possible; the multi-day probe is there to catch it before the long download.

### Runtime spike on your PC, trading run (`bench --workers 8 --set sizing.minNewOrderInr=3000 --set sizing.minAdjustmentInr=1500`)

12 CPU threads. 143 names, 4,672 rows, 3,637 sessions, 744 fills. Build 5.5 s.

| | Result |
|---|---|
| One 14.8-year run | 380 s = 104 ms per session = 26.3 s per simulated year (better than the 240 ms extrapolated from the no-trade run) |
| 8 parallel runs | slowest 4.13x slower than alone, throughput only 1.9x, wall 1,601 s |
| Strict gate, 30 tried points (130 simulations) | 12.7 h on one worker; about 6.7 h at the measured 1.9x throughput (1.6 h only at ideal scaling) |

The poor scaling is not seen on the 4-core cloud box (4 workers: 1.0x slowdown), so something on the PC saturates: candidates are thread pools of pandas/Arrow/BLAS oversubscribing 12 threads, fewer physical cores than threads, memory bandwidth, or power/thermal limits. `bench` now sets one native thread per worker by default (`--no-limit-threads` to compare) and `--scaling 1,2,4,6,8` sweeps worker counts on a 2-year window to find the best count; the strict-gate projection uses the best throughput it measured.

### Parallel scaling on your PC (12 threads), `bench --scaling 1,2,4,6,8`, 2-year window, 395 fills

| Workers | One native thread per worker | Default native pools |
|---|---|---|
| 1 | 0.99x, 1.0x | 1.00x, 1.0x |
| 2 | 0.98x, 2.0x | 2.21x, 0.9x |
| 4 | 1.13x, 3.6x | 1.91x, 2.1x |
| 6 | 1.32x, 4.6x | 4.21x, 1.4x |
| 8 | 1.55x, 5.1x | 3.61x, 2.2x |

(slowdown of the slowest worker, throughput). Decision: every process pool uses `backtest/workers.py` (`limit_threads()` in the parent, `init_worker()` in each worker). At 8 workers the strict gate (30 tried points, 130 simulations) is projected at 2.6 h instead of 5.9 h; one worker needs 13 h. A single 14.8-year run is about 6 minutes (104 ms per session). The gain from 6 to 8 workers is small, so 10 or 12 workers will probably add little (not measured).

### Bhavcopy on your PC: first complete run

Probe passed on all six sample days. Download cached 4,698 trading days (2,151 fetched in the last run plus 2,547 cached); one day failed: 2020-07-13 writes dates as `13-Jul-20`. Fixed (a two-digit year is read with `%y`). Build: 20 yearly Parquet files, 78k rows for 2007 up to 530k for 2025. Cross-check totals: NO_BHAV_ROW 1,488, NO_YAHOO_ROW 1,014, VOLUME_MISMATCH 611, PRICE_SPIKE 544, RATIO_BREAK 366. `--summary` explains what they are made of (series the missing days traded in, whole-market gaps, which breaks look like real split/bonus factors). One hypothesis to test before trusting any total: `seriesKeep` is `["EQ"]`, so a ticker-day in series BE/BZ (trade-for-trade) would show as NO_BHAV_ROW.

### Bhavcopy cross-check, first reading (your PC, after the full download)

- **NO_BHAV_ROW 1,373:** 1,195 are days the stock traded in BE/BZ (trade-for-trade), hidden by the EQ-only filter; 142 are 2025-10-21 (Diwali Muhurat), missing from the app's `nse_calendar.json` so no file was downloaded. Fix: `seriesKeep` is now `["EQ", "BE", "BZ"]` with EQ winning a duplicate. Rebuild with `--build`.
- **NO_YAHOO_ROW 1,014:** mostly Diwali Muhurat sessions Yahoo does not carry (2012-10-26, 2013-11-03, 2014-10-23, 2015-11-11, 2016-10-30, 2023-11-12). 2019-02-13 and 2019-03-29 (110 tickers each) are not Muhurat days: gaps in the Yahoo data.
- **PRICE_SPIKE 544:** median departure 1.1%, 90th percentile 3.4%, 99th 7.4%; clusters on volatile days. Most likely Yahoo's last-trade close versus NSE's 30-minute-average close, not bad data. `--summary` now splits them by size (20%+ are the ones to inspect).
- **RATIO_BREAK 366:** 143 look like splits and bonuses; 191 are 1 to 5% steps, which suggests dividend- or rights-adjusted closes. `--summary` now tests whether the stored Close is price-only (compares those breaks with Yahoo dividend ex-dates and AdjClose/Close steps). The answer decides whether the backtest's dividend credits would double count.

### Bhavcopy cross-check after the BE/BZ fix (your PC)

NO_BHAV_ROW fell from 1,373 to 177 (144 are days with no raw file, mainly 2025-10-21 Muhurat missing from the app calendar; 33 symbol-absent days). PRICE_SPIKE 562: none at 20%+, 19 at 5-20%, the rest under 5% (median 1.1%). **Stored Close is price-only**: AdjClose/Close at the first row is below 0.99 for 123 of 143 tickers (median 0.759), so the backtest's cash dividends do not double count. RATIO_BREAK 451: 143 split/bonus factors; 281 small 1-8% steps that coincide with neither an AdjClose/Close step nor a Yahoo dividend ex-date (1 each), so they are another kind of adjustment (rights issues are the first suspect); `--summary` now also tests Yahoo split dates, step duration and gives examples. `--universe-stats` measures survivorship directly: how many of each year-end's top 150 names by traded value still trade today and how many are in today's universe.

### Phase 7b: decisions and first step

Measured on your PC: 4,497 symbols ever traded, 1,529 stopped; of 2008's top 150 by traded value only 47 are in today's 143-name universe (2013: 58, 2018: 68, 2023: 81), so today's universe carries a large hindsight bias, not only a dead-name one. The small ratio steps in 2007-2009 are Yahoo-versus-NSE close noise of a few percent, not adjustments.

Decided with you: (1) liquidity-rank point-in-time universe (top 50 / next 50 / next 50 by trailing median traded value, dead names included) in `universe.mode: "pit"`, `"today"` stays the default; (2) renames linked by ISIN chain plus a manual `backtest/config/symbol_map.csv`, and a probe of NSE's symbol-change file; (3) a held name that stops trading exits at its last price, with sensitivities that haircut names that vanish with no successor by 50% and 100%; (4) bhavcopy-priced scope = symbols that were in the top 250 at any year end (plus today's names).

Built so far: `links.py` (ISIN chain, manual table, NSE file once its columns are set, cycle-safe `resolve`, review list of stopped symbols with no link) and `adjust.py` (corporate-action factors from NSE's own PREVCLOSE reference price: split-like multipliers rescale price and volume, dividends/rights move only AdjClose, anything else cuts the series and is listed), driven by `python -m backtest.universe --links | --probe-symbolchange | --validate-adjust`. `--validate-adjust` checks the derived series against Yahoo for today's names before it is trusted for dead ones. Next: the point-in-time ranking and its use in the loader and target builder (7b-2), then exits for vanished names and the haircut runs (7b-3).

### Phase 7b-1 on your PC: what the three runs found

- `--links`: 505 ISIN links; 60 stopped symbols with no link. Six of the 15 biggest are rights-entitlement tickers (`IBUL-RE`, `RIL-RE`, `UPL-RE`, `AIRTEL-RE`, `ADANI-RE`, `LLOYDSE-RE`): now excluded by `universe.excludePattern`. Most of the rest are mergers (HDFC, MINDTREE, RPL, BHARATFIN, IDFC, CAIRN): these are NOT linked (a swap ratio breaks price continuity); they leave through the vanish rule (exit at the last price). Only true renames belong in `symbol_map.csv`.
- `--probe-symbolchange`: the NSE file has no header row and company names contain commas; the layout (fields counted from the end: old, new, date; `%d-%b-%Y`) is now in `universe.symbolChange.layout`.
- `--validate-adjust` failed the first method: NSE's bhavcopy PREVCLOSE is not adjusted on an ex-date (0 of 171 Yahoo splits found; 5 false events; 140 "cuts" mostly at sessions the app calendar lacks: 2024-03-02, 2025-10-21, 2026-02-01). Replaced by `adjust.py` v2: a move over 30% is a split/bonus only if the factor is a usual one AND the volume level shifts by the same factor and stays shifted; a move with unchanged volume is a genuine crash and is kept (cutting at real crashes would delete the failures a survivorship-free universe needs); anything else cuts. `backtest.bhav --download` now also fetches benchmark sessions missing from the app calendar. Re-run `--validate-adjust` before trusting dead-name prices.

### Phase 7b-2: the point-in-time universe in the engine (built, not yet run on real data)

`python -m backtest.universe --build-pit` ranks every bhavcopy name (renames merged, `-RE` entitlements excluded) by its median traded value over the last 120 sessions (at least 60 present) on each weekly rebalance date and labels the top 50 / next 50 / next 50 Large / Mid / Small. Only names that were in the top 250 at some month end are priced: Yahoo's series where Yahoo has the name, otherwise the bhavcopy adjusted by `adjust.py`. A name with no usable series is skipped so the bands stay full, and the skipped slots are reported as "holes" per year (the survivorship bias still left in). Outputs: `backtest/data/pit/{series,membership}.parquet`, `report.json`.

Engine wiring: `PitData.members(asof)`, `Targets` (a bucket's columns change week to week) and `replay.simulate` (Context.buckets_of per day) use the membership when it is attached. Tests: with a membership equal to the static buckets, NAV and fills are identical to today mode; only members can be picked; a name's label on a date does not depend on later data. `universe.mode: "pit"` is refused until `universe.adjustValidated` is set to true, which is meant to follow a `--validate-adjust` run whose derived prices agree with Yahoo. Still to build (7b-3): exit for a held name that stops trading (last price) and the 50% / 100% haircut runs, the run command that reports today's-universe versus point-in-time results side by side.

### Split detection v2 on your PC, and what comes next

`--links`: 1,088 links (NSE file 1,056, ISIN 32); the 60 still unlinked are mergers and exits, which leave at their last price. `--validate-adjust` with the price-and-volume detector: 78 of the ~150 Yahoo splits in range matched (recall about 52%), 2 events not in Yahoo (ADANIENT's 2015 demerger, NLCINDIA 2008: precision 97.5%), close within 1% of Yahoo on 84.4% of 400k ticker-days, 38 names cut at an unresolved break. Missed ones include clean 5:1 splits (ICICIBANK 2014-12-04, KOTAKBANK 2026-01-14, ADANIPOWER 2025-09-22), so the volume test (+-40% of the factor over 10 sessions) is too strict somewhere. `--validate-adjust` now prints, for each missed split, the price move, the usual factor, the volume shift and the decision; `--tune-adjust` scores a grid (volume tolerance 0.4/0.6/0.8, window 10/20, factor tolerance 5%/8%) against Yahoo's list. No setting is chosen until those numbers are read; `universe.adjustValidated` stays false.
