# HPO — Technical Design Document

Oct 4, 2026 · @Deba · Status: draft for review · Depends on `Parameter_Exposure_Spec.md` (S1)

`hpo/` is a sibling of `app/` and `backtest/`. It searches the app's and the backtest's parameters for configurations that are robust, not merely good in-sample, and hands you a candidate to apply by hand. It places no orders, never writes `app/` or `app/data/`, and never writes `app/config/`.

## Overview

| Piece | Module | Purpose |
|---|---|---|
| Space | `space.py`, `schema/` | Typed, hierarchical `ParameterSpace` generated from the S1 register; freeze/unfreeze; reparameterisation; constraints |
| Evaluation | `objective.py`, `evalpool.py` | One continuous simulation per config, fold slicing, objectives, constraints, caching, failure handling |
| Search | `samplers.py`, `stages.py`, `study.py` | Optuna 5.0 stages: Sobol screen, multi-objective TPE, GP refinement; ask-and-tell over a process pool |
| Pareto | `pareto.py` | Feasible front, hypervolume, selection rules |
| Validity | `ledger.py`, `stats.py`, `robust.py`, `gate.py` | Effective-trial ledger, DSR/PBO/SPA, plateau and stress scoring, holdout one-shot |
| Sensitivity | `sensitivity.py` | Importance, freezing decisions |
| Promotion | `promote.py` | Candidate overlay, diff, dossier |
| Visuals | `viz/` | Self-contained HTML study report and live dashboard; every sweep outcome as a chart (section "Visual reports") |
| Tools | `cli.py`, `progress.py`, `status.py` | Commands, progress bar, status file, run locks |

## Principles

1. **Isolation.** `hpo` imports `app` and `backtest.api` only. Neither imports `hpo`. `hpo` is not copied into any image. It writes only under `hpo/data/` (git-ignored).
2. **The judge is the backtest.** No second simulator. Parity with the live app is inherited from the backtest's own tests.
3. **Statistics before optimisation.** About 13 pre-holdout years and about 744 fills limit what any optimiser can prove. The design controls how wide the search is, and how a winner is chosen, more than which sampler is used.
4. **Everything resumable.** Every finished trial is durable at once. A stopped study resumes exactly.
5. **Defaults are the baseline.** Your live configuration is trial 0 of every study, and "keep the defaults" is a valid outcome.

## Decision log

| # | Topic | Decision |
|---|---|---|
| D1 | Sequencing | Two specs: S1 exposure first, then this TDD |
| D2 | Capital | Rs 1 lakh as today; sizing minimums, name caps and risk per position are searchable so the system trades |
| D3 | Objectives | Pareto of fold-CVaR post-tax CAGR (max) and full-span post-tax max drawdown (min); ulcer index report-only |
| D4 | Feasibility | Max drawdown ≥ -30%; fills ≥ 300 and ≥ 15 per fold-year; average gross exposure ≥ 40% (proposed defaults) |
| D5 | Universe | Search on `pit` at 50% write-off; 0% and 100% as stress; re-check on `today` for the final bias estimate |
| D6 | Scope | Everything configurable; risk-limit, model-input, regulatory, design and structural classes frozen by default |
| D7 | Granularity | Strategy parameters searchable per bucket (not shared) |
| D8 | Compute | Your PC, engine unchanged; per-trial checkpoint; progress bar; `resume` and `new` |
| D9 | Promotion | Candidate file plus diff and dossier; manual apply; `hpo` never writes `app/config`; 13-week shadow period |
| D10 | Optimiser | Optuna 5.0 behind an adapter: Sobol → MO-TPE → GP refinement |
| D11 | Overfitting | Strict and enforced: cumulative effective-trial ledger, refusal past the cap, plateau selection, one-shot holdout |
| D12 | S1 scope | Numeric constants and thresholds only |
| D13 | Architecture | Own `ParameterSpace` plus a thin `backtest.api` |
| D14 | Fold aggregation | CVaR of the worst 30% of folds (proposed default) |
| D15 | Benchmark | Nifty 50 TRI for reporting (proposed default; the price index is used until a TRI source exists) |
| D16 | Re-optimisation | At most once a year; triggers prompt a review, not a retune (proposed default) |
| D17 | Visuals | Self-contained HTML reports (Plotly bundled) for every study and candidate, plus a live dashboard; 24 charts covering front, importance, landscape, robustness, stress and promotion |

D7 raises the dimension count: strategy parameters are 4 regimes × 3 buckets × 3 numbers = 36, on top of about 40 others. Section "Search protocol" explains how staging, freezing and the ledger counter this.

## Layout and data

```
hpo/
  pyproject.toml            # depends on the repo root and backtest by path, plus optuna, optunahub, cmaes, numpy, pandas, scipy, plotly (reports only)
  config/hpo.json           # budgets, caps, thresholds, paths (validated at load)
  config/studies/*.yaml     # one file per study: stage, active parameters, budgets, seed
  schema/parameters.schema.json   # generated from doc/parameter_register.csv
  space.py objective.py evalpool.py samplers.py stages.py study.py pareto.py
  ledger.py stats.py robust.py gate.py sensitivity.py promote.py
  cli.py progress.py status.py
  viz/                      # charts.py (figure builders), report.py (study report), live.py (dashboard), theme.py
  data/                     # git-ignored
    studies/<name>/journal.log        # Optuna JournalStorage
    studies/<name>/trials.jsonl       # append-only, one record per trial (also mirrored to backtest's Registry format)
    studies/<name>/returns/<id>.parquet  # post-tax daily returns per trial
    studies/<name>/status.json  checkpoint.json  run.lock
    studies/<name>/report/index.html  live.html   # regenerated, safe to delete
    candidates/<id>/report.html                    # candidate dossier with charts
    ledger/research_ledger.json       # cumulative, never decreases
    candidates/<id>/{overlay.json, diff.md, dossier.json, dossier.md}
    holdout.marker
tests/hpo/
```

Run from the repo root: `uv run --project hpo python -m hpo.cli ...`.

## Parameter system

`ParameterSpace` is the single source of truth. It is generated from the S1 register and exposes each parameter with: dotted path, kind (`int`, `float`, `ordinal`, `bool`), bounds, scale (`linear` / `log`), step (optional), class, group, stage, `conditional_on`, and `affects` (live / backtest / both). It is versioned (`schema_version`, semver); every trial record stores the version.

**Reparameterisation (constraints by construction, never by penalty)**

| Case | Encoding |
|---|---|
| `smaSlow > smaFast` | search `smaFast` and `smaGap`; `smaSlow = smaFast + smaGap` |
| Stop clamp `lo < hi` per bucket | search `lo` and `width` |
| `minAdjustmentInr ≤ minNewOrderInr` | search `minNewOrderInr` and a ratio in (0, 1] |
| Composition on a simplex, each ≥ floor | stick-breaking, 2 parameters, each weight ≥ 0.1 (floor configurable; a floor of 0 allows disabling a bucket) |
| Ladder (only if unfrozen) | first rung plus positive increments |
| BEAR score weights | fixed signs, magnitudes L1-normalised (5 → 4 dimensions) |
| Per-bucket values | `base` plus per-bucket offset, so "shared" is the zero-offset point; Stage A can then show whether offsets matter |
| Lookbacks, ADV, minimum order, ATR period | log scale |
| Regime-specific parameters when the regime never occurs in a window | marked inactive in `user_attrs`; excluded from sensitivity counts |

**Constraint order:** structural encoding → deterministic repair (round, clip to grid step) → a zero-cost pre-check using the app's own validators (`analyst.common.validate`, `risk.common.validate`) → post-simulation constraints through `trial.set_constraint`. Invalid combinations never cost a simulation. Big penalty constants are not used.

**Freezing.** Study files list the active parameters (by id, group or class). Anything not active takes its live value. Unfreezing a `risk-limit`, `model-input`, `regulatory` or `structural` parameter needs `allow_unfreeze: [path]` in the study file and is printed as a warning in every report.

**Example entries** (from the register):

```json
{"analyst.regime.smaFast":      {"kind":"int","default":50,"low":30,"high":80,"class":"tunable","group":"regime","stage":3},
 "analyst.regime.smaGap":       {"kind":"int","default":150,"low":80,"high":200,"derived":"smaSlow=smaFast+smaGap","stage":3},
 "analyst.regime.persistenceWeeks":{"kind":"ordinal","default":4,"choices":[1,2,3,4,5,6],"stage":3},
 "analyst.strategies.BULL.MidCap.top_n":{"kind":"int","low":1,"high":8,"class":"tunable","group":"signal","stage":1},
 "analyst.strategies.BULL.MidCap.lookback":{"kind":"int","low":20,"high":250,"scale":"log","stage":1},
 "analyst.composition":         {"kind":"simplex","default":[0.5,0.3,0.2],"min_each":0.1,"stage":3},
 "risk.stops.atrMultiplier":    {"kind":"float","default":3.5,"low":2.0,"high":5.0,"stage":2},
 "risk.stops.clampPct.MidCap":  {"kind":"pair","default":[0.14,0.22],"reparam":"lo+width","stage":2},
 "risk.sizing.riskPerPositionPct":{"kind":"float","default":0.0125,"low":0.005,"high":0.025,"stage":0},
 "risk.sizing.minNewOrderInr":  {"kind":"int","default":25000,"low":2000,"high":30000,"scale":"log","stage":0},
 "risk.ladder.levels":          {"class":"risk-limit","frozen":true},
 "analyst.costs.slippageBpsPerSide.SmallCap":{"class":"model-input","frozen":true,"stress":[25,50,75,100]}}
```

## Evaluation

**One simulation per config.** Parameters are fixed within a trial, so walk-forward folds are windows of one continuous run. `objective.py` calls `backtest.api.evaluate_config` once over the pre-holdout span and slices per-fold metrics from the stored daily post-tax returns. Compared with simulating each fold separately (about 10 × 150 s), one run costs about 330 s, so it is 4–5× cheaper. This also carries open positions and the ladder across fold boundaries, as in live trading. Folds are the existing rolling 5y / 1y / 1y windows with the larger of 168 and the longest allowed look-back as the purge (`backtest.api.windows`). Because there is no model fitting inside a trial, a fold here is a measurement window, not a train/test split; the true out-of-sample checks are the neighbourhood, stress, holdout and shadow stages.

**Objectives**

- `f1` = CVaR over the worst 30% of fold post-tax CAGRs (maximise).
- `f2` = full pre-holdout-span post-tax max drawdown (minimise).
- Report-only: ulcer index, Calmar, Sortino, turnover, time under water, time in market, worst stress window.

**Constraints** (reported through `trial.set_constraint`, value ≤ 0 is feasible): `dd_cap` (max drawdown ≥ -30%), `min_fills` (≥ 300), `fills_per_fold_year` (≥ 15), `min_exposure` (average gross exposure ≥ 40%, so "winning" by sitting in cash is infeasible), `aborted`.

**Aborts.** Infeasibility only, never performance: abort a run when running drawdown is worse than -35%, or when there are no fills after 3 simulated years. The trial is recorded as infeasible, not as pruned. Performance-based pruning would favour configs that did well in the first years. If a screener fidelity is ever built, it prunes only across fidelities, after verifying rank agreement.

**Caching.** Results are keyed by `(config_hash, data_hash, code_sha, seed)`; the simulation is deterministic, so a hit is exact. `Targets` (regime history and weekly picks) is memoised by the hash of the regime, selector, strategy and universe parameters, with a configurable LRU size (default 4; each entry holds panels, so memory is about 350 MB per worker plus the cache). Stages that vary only stops and sizing reuse the same targets.

**Failure mapping.** Exceptions and non-finite returns → trial `FAIL` (recorded, never retried silently). Zero fills, constraint violations, validator rejections → `COMPLETE` but infeasible. More than 5% `FAIL` in any 50-trial window aborts the study (exit 1).

**Workers.** `evalpool.py` uses `backtest.workers` (`limit_threads()` in the parent, `init_worker()` in each worker): one native thread per worker, 8 workers by default (5.1× measured throughput). Workers are pure simulators: they receive a plain dict and return a result; only the parent touches Optuna and the files. Windows `spawn`: side-effect-free imports, a `__main__` guard, the world built once per worker in the initialiser.

## Search protocol

**Stages.** Stage ids are in the register (`stage`).

| Stage | Purpose | Active parameters | Sampler | Trials (default) |
|---|---|---|---|---|
| 0 Feasibility | find sizing that trades at Rs 1 lakh | sizing minimums, name caps, risk per position (5–7) | Sobol + TPE | 64–128 |
| A Screen | rank parameters, decide what to freeze | all non-frozen, including per-bucket offsets (about 60) | Sobol (QMC) | 256–512 |
| B Search | find the Pareto region | top parameters from A, run in blocks: signal (1), stops and sizing (2), regime and composition (3, narrow bounds) | multi-objective TPE (multivariate, `group=True`, constant liar) | 300–800 per block |
| C Refine | local refinement | at most 25 most important parameters | `GPSampler` (q-batch constrained EHVI) | 100–200 |
| D Polish (optional) | local robust polish | continuous subset | CatCMAwM on a scalarised robust objective | 100–300 |

- Trial 0 of each study is the live default; 3–5 variants around it are enqueued (an approximation of a prior around the defaults).
- Switching B → C when hypervolume gain over the last 150 trials is < 1% and sensitivity is concentrated in ≤ 25 parameters. Stay on TPE if more than 30% of active parameters are categorical or conditional.
- Run blocks 1 → 2 → 3 once, then C. Every extra cycle adds to the effective-trial count.
- **Counter to D7.** With per-bucket parameters the screen is about 60 dimensions. Stage A's sensitivity (PED-ANOVA, plus Morris or Sobol indices on the Sobol points) decides, per parameter, whether to keep it active or freeze it at the live value; per-bucket offsets that do not matter collapse to the shared value. The ledger (below) counts effective trials, so a wider search directly uses up the budget.
- Budget guidance (judgement, not proof): 15–25 active dimensions need about 600–1,200 full evaluations; 40–45 need 1,500–2,500; beyond 80 is not statistically defensible with this data.

**Adapter.** `samplers.py` wraps Optuna so a sampler can be swapped (pinned `optuna==5.0.*`, API surface limited to `create_study`, `ask`, `tell`, `set_constraint`, journal storage). Optuna 5.0 is under a month old (released 7 Sep 2026); verify the exact ask-and-tell constraint semantics in the first phase.

## Statistical control

1. **Research ledger.** `ledger/research_ledger.json` is cumulative and never decreases. After each study, trials are clustered on the correlation of their daily post-tax returns (distance `sqrt((1 - ρ) / 2)`, cluster count by silhouette or inter-cluster ρ < 0.5). The cluster count is the study's effective N. Raw and effective N are recorded for every study and every `hpo` run.
2. **Cap.** The effective-N cap is 200 (`hpo.json`). A study refuses to start (exit 3) if the cumulative effective N plus its planned trials would exceed the cap; override with `--override-cap "reason"`, which is logged in the ledger and the dossier. The cap follows from the minimum-backtest-length bound MinBTL ≈ 2 ln N / SR² (Bailey, Borwein, López de Prado and Zhu): with about 13 years and Sharpe 1.0, ln N ≤ 6.4, and lower Sharpe lowers the allowed N sharply. It is a necessary, not sufficient, condition.
3. **Corrected gate.** The existing gate thresholds stay, with these changes:
   - Deflated Sharpe uses the cumulative clustered effective N (reported also at 2N).
   - PBO via CSCV (S = 16) runs over the full trial return matrix (or medoids of up to 500 clusters), not over the neighbours of one point.
   - OOS/IS requires IS > 0 and an absolute floor (median fold CAGR > 0).
   - Neighbourhood tolerances are per metric: CAGR within 20% relative or 2 pp; drawdown within 3 pp absolute; ulcer within 25%. Neighbours are ±1 step on the top-10 parameters plus 10 random multi-parameter perturbations.
   - New checks: minimum fills, ranking stable at 2× costs and 100% write-off, SPA/Romano-Wolf against the live default (p ≤ 0.10, or the candidate is labelled "adopt for feasibility or robustness only").
4. **Plateau, not peak.** Finalists (the top 20–30 on the feasible front) are re-scored on a neighbourhood: the 25th percentile of CAGR and worst-quantile drawdown over about 16 perturbations (±1 integer step, ±10% float, adjacent ordinal), plus stress runs: slippage × 2, charges × 1.3, write-off 100%, +1-day execution delay, 5% of names removed, `today` vs `pit`. About 500 simulations (about 9 hours at the measured throughput).
5. **Regime and stress windows.** Metrics by regime (using the default classifier) and for the named stress windows. A candidate fails when its BEAR or any stress-window drawdown is more than 5 pp worse than the default's.
6. **Selection.** From the robust subset: the highest Calmar, tie-broken by ulcer index (default). The medoid of the plateau is preferred to parameter averaging, which can create an untested config.
7. **Holdout.** The last 2 years stay locked (`HoldoutRead`). `score_holdout` runs once, for one parameter set, against a pre-registered pass criterion. With a 2-year standard error on Sharpe of about 0.85, it detects catastrophic failure only.
8. **Exploit audit** (finalist vs default; part of the dossier): 2-decimal cash handling, retry-window re-pricing, vanished-name P&L share, bhavcopy-derived names without dividends, Muhurat days, missing 2019 index days, surveillance-off trades, unmodelled bands/circuits/volume caps, charges at today's rates, the draft tax table across the 2018 and 2024 rules, universe holes, any parameter at a bound, and cliffs (a ±1 step moving CAGR by more than 3 pp).
9. **Honest edge.** HPO is unlikely to yield a statistically demonstrable improvement over sensible defaults on this data (about 0.28 standard error on a 13-year Sharpe). Realistic value: a configuration that trades at Rs 1 lakh, avoidance of fragile regions, a sensitivity map, and perhaps lower drawdown at similar CAGR. The dossier states this.

## Operations

**Commands** (run from the repo root, `--check` validates config, schema and imports with no network and no writes; exit codes 0 ok, 1 failed, 2 busy, 3 gate not met or cap/holdout refusal):

```
python -m hpo.cli space check|show [--stage N]
python -m hpo.cli study new   --config config/studies/s1.yaml          # clean study
python -m hpo.cli study run   --name s1 [--trials N] [--workers 8]     # starts or continues
python -m hpo.cli study resume --name s1
python -m hpo.cli study status --name s1
python -m hpo.cli sensitivity --name s1
python -m hpo.cli front       --name s1 [--select calmar|knee]
python -m hpo.cli robust      --name s1 [--top 30]
python -m hpo.cli gate        --candidate <trial_id>
python -m hpo.cli holdout     --candidate <trial_id>                    # once only
python -m hpo.cli promote     --candidate <trial_id>
python -m hpo.cli ledger show
python -m hpo.cli report      --name s1 [--open]                        # HTML study report
python -m hpo.cli report      --candidate <trial_id> [--open]           # candidate report
python -m hpo.cli live        --name s1                                 # auto-refreshing dashboard while a study runs
```

**Checkpoint, resume, new.** The parent writes each finished trial immediately to the Optuna journal and to `trials.jsonl` plus its returns file (the Registry format of `backtest/trials.py`, which stays the audit source). `checkpoint.json` records the study config hash, sampler seed and counters. On start the parent rebuilds the Optuna state from the journal, cross-checks every completed trial against `trials.jsonl` by hash, re-enqueues trials that were in flight when the machine stopped (determinism makes the re-run identical) and continues. `study resume` and `study run` on an existing name continue; `study new` always creates a new directory, so a fresh run never overwrites an old one. If the study config hash, `data_hash`, `code_sha` or schema version changed, resume refuses (exit 3) and tells you to start a new study; sampler version changes warn. A kill loses at most the in-flight trials (≤ 8 workers × about 6 minutes). Mid-simulation snapshots are out of scope.

**Progress.** A terminal progress line, updated per finished trial: trials done / planned, feasible count, FAIL count, best CAGR and drawdown on the front, hypervolume, trials per hour, elapsed time and ETA, cumulative effective N against the cap. `status.json` carries the same numbers for a quick look, written atomically.

**Locks.** A per-study `run.lock` (PID and time; stale after 6 hours) and a global holdout lock. A second `run` on the same study exits 2.

**Determinism.** Seeds, config hash, data hash, code sha and package versions are recorded per trial. With constant liar or q-batch acquisition a parallel study is reproducible from its journal, not bit-identical on a fresh re-run; the Registry is the audit trail.

## Visual reports

Every sweep outcome has a chart. `hpo report` reads only the files under `hpo/data/` (journal, `trials.jsonl`, returns, ledger, candidate dossiers), so it works on a finished, running or stopped study and never touches the simulation. Output is one self-contained HTML file per study (and per candidate), opened from disk, with hover values, light and dark themes, and no network access at view time. Charts use Plotly (bundled into the file); a PNG/SVG export of each chart is written next to the report for pasting into notes.

**Study report (`report/index.html`)**

| # | Chart | What it answers |
|---|---|---|
| 1 | **Run summary tiles**: trials done, feasible share, FAIL count, hypervolume, best CAGR and drawdown, effective N vs the cap of 200, stage and sampler | Is the study healthy and how much budget is left |
| 2 | **Pareto front scatter**: all feasible trials (CAGR on y, max drawdown on x), the front highlighted, the live default marked, the dd-cap line at -30%, colour by trial order | Where the trade-off lies and whether anything beats the default |
| 3 | **Hypervolume vs trials**, with stage boundaries and the "< 1% gain over 150 trials" stop rule marked | When to switch stage or stop |
| 4 | **Feasibility funnel**: trials → valid → enough fills → exposure ≥ 40% → drawdown ≥ -30% | Which constraint is rejecting configs, and whether the space is mostly infeasible at Rs 1 lakh |
| 5 | **Parameter importance bars** (PED-ANOVA per objective; Morris or Sobol indices for Stage A) with the freeze/keep decision coloured | Which parameters matter, which to freeze |
| 6 | **Slice plots** per top parameter (objective vs value, front trials highlighted) and **2-D contour or heatmap** for the top interacting pairs (for example top_n × lookback, ATR multiplier × clamp width) | Shape of the landscape: plateau, cliff or spike |
| 7 | **Parallel-coordinates** plot of front trials over the top 12 parameters | What the good configs have in common |
| 8 | **Per-bucket offset plot**: each per-bucket parameter's offset from the shared value, with its importance | Whether per-bucket freedom (D7) earns its dimensions |
| 9 | **Trial timeline**: objective vs trial number with the running best and sampler stage bands | Whether the search is still learning |
| 10 | **Run-time chart**: trials per hour and ETA | Planning the next night's run |

**Robustness and candidate report (`candidates/<id>/report.html`)**

| # | Chart | What it answers |
|---|---|---|
| 11 | **Equity curves** (post-tax NAV, log scale) of the candidate, the live default and the benchmark, with the holdout shaded and stress windows banded | How it actually behaved |
| 12 | **Underwater (drawdown) plot** for candidate vs default, ladder rung and regime strip underneath | Drawdown depth, duration and what the ladder did |
| 13 | **Fold bars**: post-tax CAGR and max drawdown per rolling fold, candidate vs default, with the CVaR₃₀ level marked | Consistency across time, not just the average |
| 14 | **Stress-window bars**: drawdown and return in each named window, plus the +5 pp failure line | Where it is fragile |
| 15 | **Plateau heatmaps**: the neighbourhood grid around the candidate for the two most important parameters, CAGR and drawdown side by side, the candidate circled | Peak versus plateau |
| 16 | **Neighbour distribution**: histogram of CAGR and drawdown over the 16 perturbations with the 25th percentile and the candidate marked | How much a small parameter nudge moves results |
| 17 | **Stress-test table as bars**: baseline, slippage × 2, charges × 1.3, write-off 0/100%, +1 day delay, 5% names removed, `today` vs `pit` | Sensitivity to the simulator's assumptions |
| 18 | **PBO chart**: logit rank-correlation histogram from CSCV with PBO and the 0.20 limit | Probability that selection is misleading |
| 19 | **DSR vs effective N**: the deflated Sharpe as N grows (N and 2N marked) with the 0.95 limit | How the multiple-testing penalty bites |
| 20 | **Regime breakdown**: return, drawdown and share of time per BULL/TREND/WEAK/BEAR, candidate vs default | Whether the gain comes from one regime |
| 21 | **Trade profile**: fills per year, turnover, position count, exposure over time, cost drag | Whether it trades like a swing system and at Rs 1 lakh |
| 22 | **Exploit-audit panel**: pass/fail grid with the metric behind each cell, parameters at a bound flagged on a bounds bar | Evidence the optimiser did not exploit the simulator |
| 23 | **Parameter diff chart**: old vs new value for every changed key on its bounds range, coloured by class (risk-limit and model-input changes in red) | What exactly would change in your config |
| 24 | **Holdout and shadow panel**: holdout result against its pre-registered criterion; during shadow, tracking gap vs the ±2 pp band over the 13 weeks | Promotion status |

**Live dashboard (`hpo live`)** is a local page that re-reads `status.json` and `trials.jsonl` every 10 seconds while a study runs: tiles (1), front (2), hypervolume (3), funnel (4), trial timeline (9) and run time (10). It mirrors the terminal progress bar for a second screen and needs no server beyond a static file (`<meta refresh>`), so it also works after a resume.

**Design rules.** One colour per role used the same way everywhere (candidate, default, benchmark, infeasible, front); infeasible trials greyed, never hidden; units and bounds on every axis (percent for CAGR and drawdown, trading days for windows); each chart has a one-line "how to read" caption and the criterion it is judged against; a data table underneath each chart is available for copying; charts degrade to a table when a study has too few trials. Small multiples are used in preference to a combined chart when more than four series would overlap.

**Data discipline.** Reports show feasible and infeasible trials together but label the effective-N count and stage on every page. A report generated before the holdout is scored never contains holdout data; the holdout panel appears only after `holdout` has run, and it is drawn once. Reports never contain secrets or paths outside `hpo/data`.

## Promotion

`promote` accepts a candidate only if the gate, robustness checks, exploit audit and one-shot holdout have passed. It writes `candidates/<id>/`:

- `overlay.json`: the changed values only, as dotted paths for `risk.json` and `analyst.json`;
- `diff.md`: old vs new value for every changed key, with the register's class and note;
- `dossier.md/json`: objectives and front position, plateau and stress results, gate report, effective N, exploit audit, holdout result, shadow limits, and the honest-edge statement.

You apply the overlay by hand and bump `config_version` in your changelog. The candidate then runs in the existing shadow portfolio for 13 weeks (13 weekly rebalances) with limits fixed in the dossier beforehand: cumulative tracking gap between shadow-realised and backtest-replayed results within ±2 pp, and no breach of the backtest's 95th-percentile drawdown. This tests fidelity, not edge. Roll back if realised drawdown passes the 95th-percentile figure, shadow diverges by more than 2 pp, or an exploit is found. Re-optimise at most once a year; triggers (an unseen regime, a drawdown breach, a code or cost-model change) prompt a review, not an automatic retune, because each retune adds to the ledger.

## Configuration — `hpo/config/hpo.json`

```json
{
  "paths": {"data": "hpo/data", "register": "doc/parameter_register.csv", "backtestConfig": "backtest/config/backtest.json"},
  "universe": {"selection": "pit", "writeOff": 0.5, "stressWriteOffs": [0.0, 1.0], "finalCheck": "today"},
  "objectives": {"cagr": {"fold": "cvar", "worstShare": 0.30}, "drawdown": "span"},
  "constraints": {"maxDrawdown": -0.30, "minFills": 300, "minFillsPerFoldYear": 15, "minAvgExposure": 0.40, "abortDrawdown": -0.35, "abortNoFillYears": 3},
  "ledger": {"effectiveNCap": 200, "clusterRhoMax": 0.5},
  "robust": {"top": 30, "neighbours": 16, "quantile": 0.25, "intStep": 1, "floatRel": 0.10,
             "stress": {"slippageMult": 2.0, "chargesMult": 1.3, "writeOff": 1.0, "delayDays": 1, "dropNames": 0.05}},
  "gate": {"pboMax": 0.20, "dsrMin": 0.95, "oosFloorCagr": 0.0, "spaP": 0.10, "cagrRel": 0.20, "cagrAbs": 0.02, "ddAbs": 0.03, "ulcerRel": 0.25, "neighbourShare": 0.80},
  "shadow": {"weeks": 13, "trackingGapPp": 2.0},
  "compute": {"workers": 8, "targetsCache": 4, "failShareAbort": 0.05, "seed": 1},
  "optuna": {"version": "5.0.*", "storage": "journal"}
}
```

Validated at load; unknown or out-of-range values raise (as `backtest/config.py` does).

## Compute budget (estimates from measured numbers)

At about 330 s per pre-holdout run and 5.1× throughput on 8 workers: about 55 full evaluations per hour, about 550 per night (10 h), about 9,000 per week.

| Plan | Evaluations | Time on your PC |
|---|---|---|
| Stage 0 | 64–128 | 1–2 h |
| Stage A (about 60 dims) | 256–512 | 5–9 h |
| Stage B, three blocks | 900–2,400 | 16–44 h |
| Stage C | 100–200 | 2–4 h |
| Robust re-evaluation (30 × 17) | about 510 | about 9 h |
| Realistic total | about 1,800–3,700 | about 3–7 nights |

Per-bucket parameters push the plan toward the top of these ranges. The engine is unchanged (decision), so a vectorised screener (10–50× faster) stays a conditional later phase; build it only if more than 2,000 evaluations per stage are needed and a screener-vs-full Spearman correlation reaches 0.8. Memory is about 8 × 350 MB plus the parent (0.5–1 GB while fitting the GP).

## Tests (`tests/hpo`, offline, synthetic, same style as `tests/backtest`)

| Area | Test |
|---|---|
| Space | every sampled point satisfies all constraints (property test); defaults equal the live config; reparameterisations round-trip |
| Parity | `evaluate_config` with default overrides equals `backtest.run.evaluate` (NAV, fills) |
| Objectives | fold slicing equals separate per-window metric computation from the same returns; constraint signs |
| Known optimum | a synthetic landscape with a plateau and a narrow spike: the pipeline selects the plateau within the trial budget |
| Known overfit | 500 noise series: PBO ≈ 0.5, DSR < 0.95, the gate fails |
| Ledger | effective N never decreases; clustering merges near-duplicate trials; the cap refuses a study |
| Holdout | `HoldoutRead` on any holdout access; a second `holdout` exits 3 |
| Determinism | same key gives byte-identical metrics; fixed-seed sequential sampler gives the same sequence |
| Resume | kill mid-study, resume: journal and `trials.jsonl` consistent, no duplicate hashes, final front equals an uninterrupted sequential run |
| Failure | zero fills → infeasible; NaN → FAIL; more than 5% FAIL aborts |
| Reports | the report builds from a fixture study (mid-run, finished, with and without a holdout) and contains every chart in the table; no holdout data before `holdout` has run; the file opens with no network; charts degrade to tables on tiny studies |
| Isolation | importing `hpo` leaves `app` and `backtest` unchanged; nothing is written outside `hpo/data` (file-system diff) |
| Windows | 8 workers × 16 trials smoke: no orphans, locks released |

## Known limits and risks

| Risk | Mitigation |
|---|---|
| Selection bias on one historical path | effective-N ledger and cap, cumulative DSR, PBO over the full matrix, plateau selection |
| Per-bucket granularity (D7) inflates dimensions | Stage A sensitivity and freezing; per-bucket offsets collapse to shared when unimportant; budget accounting |
| Regime overfit (4 regime parameters, 5 BEAR weights) | narrow bounds, BEAR weights normalised, regime and stress checks |
| Simulator exploits | exploit audit, stress, cliff and bound checks |
| Holdout leakage | structural refusal, global lock, ledger |
| Capital artefact at Rs 1 lakh (at most about 4 positions at Rs 25,000 minimum) | results labelled capital-specific; re-run when capital changes |
| Optuna 5.0 API churn | pinned, adapter, verified in phase 3 |
| Live config differs from shipped | S1 task: diff your live config folder; HPO defaults are your live values |
| Universe holes and write-off assumptions | `pit` selection with stress, `today` re-check |
| Unmodelled surveillance, bands, circuits | labelled; exploit audit flags trades in such names |

## Phases

| Phase | Work | Exit |
|---|---|---|
| S1 | Parameter exposure spec implemented; register; `backtest.api` | default parity tests green |
| 1 | `hpo` skeleton, `space.py`, `objective.py`, `evalpool.py`, `study.py` with Sobol; checkpoint/resume/progress; isolation test | resume-equality and parity tests green; Stage 0 run |
| 2 | `sensitivity.py`; Stage A | importance ranking; frozen list agreed with you |
| 1b | `viz/`: live dashboard and the study-report charts 1–10 (built early so every sweep from Stage 0 onward can be read visually) | report builds on a fixture and on a real Stage 0 run |
| 3 | Optuna 5.0 TPE and GP samplers, constraints, `pareto.py`, `ledger.py` | known-optimum test passes; Stage B/C run |
| 4 | `robust.py`, `stats.py`, `gate.py`, exploit audit, candidate-report charts 11–24 | known-overfit test passes; candidate or "keep defaults" |
| 5 | `promote.py`, holdout one-shot, 13-week shadow | dossier complete; rollback rehearsed |
| 6 | Annual re-optimisation runbook | ledger tracked |
| Optional | Vectorised screener | only on the conditions above |

## Inputs needed from you

- Your live `C:\ProgramData\...\config` values, if they differ from the shipped configs (this session could not see them).
- Bounds for new S1 keys at register review.
- Confirmation of the proposed defaults: D4 constraint values, D14, D15, D16, the effective-N cap of 200.

## Sources

Research report of 4 Oct 2026 (Optuna 5.0 release notes and docs; Watanabe 2023 TPE tutorial; Ozaki et al., MOTPE; Hvarfner et al., ICML 2024; Hamano et al., CatCMA with Margin; Bailey et al., Pseudo-Mathematics and Financial Charlatanism and The Probability of Backtest Overfitting; Bailey and López de Prado, Deflated Sharpe Ratio; Harvey, Liu and Zhu 2016; Arian et al. 2024 on CPCV). Library facts are as of 4 Oct 2026 and may change; Optuna 5.0 is weeks old.
