# Parameter Exposure (S1) — Specification

Oct 4, 2026 · @Deba · Status: draft for review · Prerequisite of `HPO_TDD.md` (S2)

S1 is a behaviour-preserving change to `app/` and `backtest/`. It turns every return-affecting number that is still hardcoded into a config key whose default equals today's value, records every tunable and non-tunable number in one **parameter register**, and adds one public evaluation function to `backtest/` for `hpo/` to call. It adds no optimiser and no `hpo/` code. With the shipped configs, NAV, fills and signals stay byte-identical.

## 1. Why

`backtest/config/params.json` tunes 43 parameters, but (a) a number of return-affecting values live in code, not config, and (b) many existing config keys are not in `params.json`. An optimiser can only move what is exposed. The audit below is from the repository source (shipped configs), not only the TDDs. Local settings on your PC (`.env`, the live config folder, your real `cash_flows.csv`) were not available, so a first S1 task is to diff your live config folder against the shipped one.

## 2. Principles

1. **Defaults reproduce today exactly.** Each new key ships with the value now in code. A missing key falls back to the same default (the pattern already used by `regime.windows_of`).
2. **Validated at load.** New keys are range-checked in `analyst.common.validate` / `risk.common.validate`. `Schema.apply` already reruns these, so an invalid point is rejected before a run.
3. **Numbers only.** No new algorithm variants (decision: numeric constants and thresholds only). `atrMethod` and `priceBasis` stay limited to what is implemented.
4. **No change to live behaviour** unless you edit a key. Docker images and Task Scheduler jobs are untouched.
5. **One register.** Every number, tunable or not, is classified once.

## 3. Hardcoded numbers to expose (return-affecting)

| # | Where | Today | New key (default) | Effect |
|---|---|---|---|---|
| H1 | `selector.py` `BEAR_ROWS`, `iloc[-21]`, `iloc[-64]`, `iloc[-20:]` | 70 rows; 20-day and 63-day returns; 20-day hit rate and volatility; 63-day drawdown | `selector.bearScore.windows` `{shortDays: 20, longDays: 63, hitDays: 20, volDays: 20, ddDays: 63}`; minimum rows derived as `longDays + 7` (70 by default) | BEAR ranking |
| H2 | `selector.py` `momentum > 0` | 0 | `selector.minMomentum` (0.0) | Candidate filter, all regimes |
| H3 | `selector.py` `price > trend` | strict, no margin | `selector.trendBuffer` (0.0): price must exceed `trend × (1 + buffer)` | Candidate filter |
| H4 | `selector.py` BEAR tier `mom20 > 0 and mom63 > 0` | 0 | `selector.bearScore.confirmThreshold` (0.0) | BEAR tiering |
| H5 | `regime.py` `pct_change(mom) > 0` | 0 | `regime.momentumThreshold` (0.0) | Raw regime BULL vs TREND |
| H6 | `regime.py` `UNKNOWN_EXTRA` | 9 | `regime.unknownExtra` (9) | Warm-up length |
| H7 | `sizer.py` `max(..., 0.025)` | hidden floor of 2.5% of NAV in the no-trade band | `sizing.noTradeBand.floorPct` (0.025) | Rebalance trimming and top-ups; today `absolutePct` below 2.5% has no effect |
| H8 | `ladder.py` one rung per week; restart at `top - 1` | 1; top − 1 | `ladder.reRisk.rungsPerWeek` (1), `ladder.restartRungOffset` (1) | Step-up speed; restart level |
| H9 | `shadow.py` `LOOKBACK_DAYS` | 7 | `shadow.carryOverDays` (7); `backtest.json fill.carryOverDays` reads it by default | Retry of unfilled signals |
| H10 | `backtest/params.py` `MIN_PURGE` and `backtest/config.py` `MIN_PURGE_DAYS` | 168 twice | one constant, read from `walkforward.purgeDays` | Fold purge |
| H11 | `evaluator.py` `DAYS = 252`, `backtest/replay.py` run time 21:00 | 252; 21:00 | `evaluator.tradingDaysPerYear` (252); backtest replay clock stays internal | Metric scaling only |

Not exposed on purpose (operational, no effect on returns): `RETRY_MINUTES`, `SESSION_FINAL`, lock/backup/purge retention days, `JUMP = 0.15` (NAV jump warning), the lower-circuit warning offset, the 52-week cooldown file prune, data-load padding (`1.6 × window + 10`, `+5` row buffers), rounding precision.

Dead keys found: `stops.atrMethod` and `stops.priceBasis` are in `risk.json` but no code reads them. S1 validates that they hold the only implemented values (`sma`, `AdjClose`) and fails otherwise, so a user cannot believe they changed anything.

## 4. Existing config keys not yet in `params.json`

These already work; S1 only registers them (and adds them to the HPO space).

| Group | Keys |
|---|---|
| Composition | `analyst.composition.*` (simplex over Large/Mid/Small; a weight of 0 disables a bucket) |
| Selection | `selector.momentumSkipDays`, `selector.liquidity.windowDays`, `strategies.<regime>.<bucket>.top_n / lookback / stock_trend_ma` per bucket (the current `params.json` shares them across buckets) |
| Sizing | `sizing.noTradeBand.relative`, `.absolutePct`, `liquidity.advDays`, `liquidity.maxParticipationPct.*` |
| Exposure | `exposure.regimeCap.*` (inert today: all 1.0) |
| Ladder | `ladder.reRisk.consecutiveWeeks`, `.navAboveMinOfPreviousDays`, rung 4 values, `ladder.levels[*].maxInvestedPct` |
| Tax deferral | `tax.deferral.windowDays`, `.minGainPct`, `.requireAboveTrend` |
| Re-entry | `cooldown.reentryAboveStopClose` |
| Funds | `sizing.countSaleProceeds` |

## 5. Parameter register

The register is `doc/parameter_register.csv` (generated, reviewed by hand) with one row per number:

`path, file, current, kind, bounds, scale, class, group, affects (live / backtest / both), parity_test, note`

Classes:

| Class | Meaning | Default in HPO |
|---|---|---|
| `tunable` | Strategy, stop and sizing numbers whose value is a judgement the data can inform | searchable |
| `risk-limit` | Ladder drawdown rungs, heat cap, cash buffer, hard name caps | frozen; unfreeze per study explicitly, with a report warning |
| `model-input` | Costs, slippage, tax-lot rules, fill model, write-off | frozen; used for stress only |
| `regulatory` | Tax rates and schedule, surveillance blocks, 12-month rule | frozen |
| `design` | Capital, holdout, window, purge, universe mode, seed | frozen |
| `structural` | Metadata thresholds (2T/500B/100B, topN, `minInceptionDays`), rebalance weekday and timing | frozen; `today`-mode sensitivity only |
| `operational` | SMTP, throttle, retries, paths, schedules, retention | excluded |

Initial classification of the main groups:

| Group | Class |
|---|---|
| `regime.smaFast / smaSlow / momentumDays / persistenceWeeks`, H5, H6 | tunable (regime stage, narrow bounds) |
| `composition` | tunable (simplex) |
| `strategies.*.*` (top_n, lookback, trend MA), `momentumSkipDays`, `minAdvCr`, `liquidity.windowDays`, H1–H4 | tunable |
| `stops.atrPeriod / atrMultiplier / clampPct`, `cooldown.stopTradingDays` | tunable |
| `sizing.riskPerPositionPct`, name caps, `minNewOrderInr`, `minAdjustmentInr`, `noTradeBand.*`, H7 | tunable (name caps bounded by policy max) |
| `exposure.regimeCap.*` | tunable, each in [0, 1] |
| `ladder.reRisk.*`, H8 | tunable, narrow |
| `ladder.levels[*]`, `heat.capPct`, `sizing.cashBufferPct` | risk-limit |
| `costs.*`, `slippageBpsPerSide`, `fill.*`, vanish write-off | model-input |
| `tax.deferral.*` | tunable only for `windowDays` and `minGainPct`; rest regulatory |
| `tax.rates`, `tax.schedule`, `surveillance.*` | regulatory |
| `capital.*`, `window`, `walkforward`, `universe.*`, `compute` | design |
| `config.json filter.*`, `rebalance.*` | structural |
| everything under `paths`, `mail`, `broker`, `ledger`, `signals`, `lock`, `gate` (run gate) | operational |

## 6. Public backtest API

One new module `backtest/api.py`, the only thing `hpo/` imports from `backtest/`:

```python
def build_world(cfg_overrides: dict | None = None) -> World: ...
def evaluate_config(world, risk: dict, analyst: dict, start: str, end: str,
                    *, haircut: float = 0.0, capital: float | None = None) -> EvalResult: ...
# EvalResult: post-tax daily returns, NAV, fills (DataFrame), taxes, metrics, hashes (config, data, code)
def windows(world) -> Windows          # holdout, tuning end, folds, purge
def holdout_guard(...)                 # the existing marker/one-shot logic
```

`evaluate_config` takes the full `risk` and `analyst` dicts, so the caller applies its own overrides and validates with the app's validators. It does not use the grid-based `Schema`. It is a thin wrapper over `replay.simulate`, `tax.lots / assess / post_tax_curve` and `evaluator.perf`, the code `Session.evaluate` already uses, so results match. It keeps a keyed Targets cache of configurable size (the current `Session._targets_for` holds 4).

## 7. Tests (S1 exit criteria)

1. **Default parity.** With the shipped configs: root `uv run pytest` and `uv run --project backtest pytest -c backtest/pyproject.toml` stay green, and a golden 2-year run (`bench` window) gives byte-identical NAV, fills and signals before and after S1.
2. **Per-key effect.** Each H1–H11 key, set to a non-default value, changes behaviour in the expected direction in a unit test; set to its default, it changes nothing.
3. **Validation.** Out-of-range and wrong-type values are rejected by the existing validators; `atrMethod`/`priceBasis` reject unimplemented values.
4. **API parity.** `evaluate_config` returns the same NAV, fills and post-tax returns as `run.evaluate` on the same inputs.
5. **Register completeness.** A test fails if a numeric literal in `app/analyst`, `app/risk` or `backtest/{replay,fills,targets,tax}.py` (AST scan, with an allow-list for 0, ±1, rounding, unit constants) is neither a config key nor listed in the register as intentionally fixed. This is the CI guard against new hidden numbers.

## 8. Out of scope

New strategy logic, new ATR or ranking variants, a rebalance-weekday option, changes to cost or tax rules, the incremental stop engine (decision: engine unchanged), any `hpo/` code.

## 9. Open items for you

- Diff your live `C:\ProgramData\...\config` against the repo's shipped configs (the cloud session could not see it) and list any differing values; the HPO defaults must be your live values.
- Confirm that `exposure.regimeCap` may be searched in [0, 1].
- Confirm bounds for new keys when the register is reviewed.

## 10. Implementation notes (S1 as built)

Decisions taken while implementing, on top of the sections above:

- **Where the new keys live.** H1 and H4 are `selector.bearScore.windows` / `.confirmThreshold` (the five weights keep their names `mom20 ... dd63` even when a window changes); H5 and H6 are `regime.momentumThreshold` / `regime.unknownExtra`; H2 and H3 are `selector.minMomentum` / `selector.trendBuffer`; H7 is `sizing.noTradeBand.floorPct`; H8 is `ladder.reRisk.rungsPerWeek` and `ladder.restartRungOffset`; H9 is `shadow.carryOverDays`; H11 is `evaluator.tradingDaysPerYear`. Every one is optional in a config (a missing key falls back to the old value, defined once in code) and shipped with the old value.
- **H1 minimum rows** are the longest of the five windows plus 7 (70 by default); `rows_needed` follows it.
- **H6** also moves the `regime.minRows` rule to `max(smaSlow, momentumDays) + unknownExtra + 1`.
- **H8** `rungsPerWeek` never steps below the drawdown floor; `restartRungOffset` is 1 to the number of ladder levels.
- **H9** `backtest.json fill.carryOverDays` is now `null` by default and then reads `risk.json shadow.carryOverDays`; an integer still overrides it.
- **H10** one floor, `backtest.config.MIN_PURGE_DAYS` (168), read by both `config.validate` and `params.Schema.required_purge`. `walkforward.purgeDays` is still validated against it, and the effective purge is still lifted to the longest look-back in `params.json`.
- **H11** annualisation only. The `trailing252` window keeps its length of 252 sessions.
- **Dead keys.** `stops.atrMethod` must be `sma` and `stops.priceBasis` must be `AdjClose`, or `risk.validate` fails.
- **params.json is not edited in S1.** Section 4 keys are registered with proposed bounds in `doc/parameter_register.csv`; S2 builds its search space from the register (the composition simplex and per-bucket parameters cannot be expressed as the scalar low/high/step grid of `Schema`).
- **Register.** `python -m backtest.register --write` generates the CSV from the shipped configs and the code scan; the classification rules and the list of intentionally fixed literals are in `backtest/register.py`. The AST guard covers the return-affecting modules only (analyst selector, regime, costs, signals, common; risk sizer, ladder, stops, monitor, shadow, tax, nav, evaluator, run, common; backtest replay, fills, targets, tax); broker, ledger, probe, secrets, journal, surveil and surveillance are operational and not scanned. `validate` function bodies are skipped.
- **API.** `backtest/api.py` as in section 6, plus `targets_cache`, `data_hash` and `point_key`. `backtest.run.evaluate` and `trials.Session.evaluate` now call `api.evaluate_config`, so the parity of test 4 holds by construction and is also checked against the old inline pipeline.
- **Golden run (test 1).** `tests/backtest/golden.json` holds digests of two synthetic windows recorded before any S1 edit; `tests/backtest/test_golden.py` compares them. On real data run `python -m backtest.golden` before and after and compare the printed JSON.
- **Still open (section 9).** Diffing your live config folder, and confirming the `exposure.regimeCap` search range and the proposed bounds in the register.

