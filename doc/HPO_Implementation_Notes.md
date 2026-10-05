# HPO — implementation notes (phases 1, 1b, 2, 3)

Built from `doc/HPO_TDD.md`. This records what was decided while reading the TDD against the repo, and where the code differs from the TDD text.

## Decisions (answered by the owner)
- Scope of the first drop: phases 1, 1b and 2 (skeleton, space, objective, pool, Sobol study with resume/progress, charts 1-10, live dashboard, sensitivity). Samplers beyond Sobol/TPE, robustness, gate, holdout, promotion and candidate charts 11-24 are later phases (their CLI commands exit 1 with a message).
- `hpo` reaches `backtest` only through `backtest/api.py`, which gained thin re-exports (`limit_threads`, `init_worker`, `perf`, `pbo_cscv`, `deflated_sharpe`, `sharpe`, `config_hash`, `deep_merge`), `base_configs()`, `registry()`, a `monitor` hook (`replay.simulate` and `evaluate_config`, default None = unchanged behaviour) and `windows(start=, required_purge=)`.
- Baseline (trial 0) = the shipped `app/config`; bounds come from the register; a bound that excludes the live value is widened to it (`minNewOrderInr` 2000..10000 -> 2000..25000). Bounds the register lacks are in `hpo/config/space_extra.json` (proposed, for review).
- Effective-N cap before a study: cumulative effective N + planned trials x observed effective/raw ratio (default 0.3 before history) must be <= cap; `--override-cap "reason"` is logged.

## Where the code differs from, or fills in, the TDD
- The register has no `stage` column; stages are assigned by group in `space_extra.json` (stage 0 = the six sizing dimensions).
- Per-bucket strategy values: base (first bucket) + offsets for the other two buckets = 36 dimensions, the TDD's "4 x 3 x 3". The space has 102 dimensions in all (93 tunable, 9 risk-limit frozen); the Stage A screen over `class:tunable` is therefore ~93 dimensions, above the TDD's "~60" and near its "beyond 80 is not defensible" line. Offsets can be excluded in the study file.
- Folds, purge and start come from the whole space (longest look-back 250), so fold boundaries never change between studies.
- f1 scores the 1-year fold windows only, while f2 covers the full pre-holdout span including the first (5-year "train") years; this follows the TDD but is an inconsistency worth revisiting.
- Trials that cannot be scored (rejected point, aborted run) carry placeholder objectives (-100% CAGR, 100% depth) and a violated constraint; they are never read as performance and are greyed in reports.
- PED-ANOVA importances are re-normalised to shares; Morris/Sobol indices are not implemented (Spearman is the cross-check).
- Charts: Plotly bundled inline; a per-chart SVG export is the Plotly toolbar button, not a file written next to the report (no server-side image engine).
- Resume refuses on a changed `code_sha`, so any commit during a long study needs a new study (as the TDD says).
- Not run here: the sandbox has no `app/data`, so engine tests use the synthetic world and real timing/Stage 0 runs happen on your PC.

## Phase 3 (samplers, constraints, front, stages)
- Samplers: `sobol` (QMC), `tpe` (multivariate, `group=True`, constant liar) and `gp` (Optuna `GPSampler`, deterministic objective). Constraints reach the samplers through `trial.set_constraint` (Optuna 5.0; no `constraints_func`); verified on a known landscape: TPE ends with 100% feasible trials in the last half against about 60% for Sobol, and GP handles two objectives plus constraints.
- `gp` needs PyTorch, an optional extra: `uv sync --project hpo --extra gp`. PyPI's default Linux/Windows wheel is the CUDA build (about 2.5 GB with its libraries); on your PC install the CPU build instead (`uv pip install torch --index-url https://download.pytorch.org/whl/cpu` inside `hpo/.venv`). The sandbox's index for that URL was blocked, so the GP test ran here with the CUDA wheel. GP suggestions slow down as trials grow (about 0.5 s per trial at 40 trials, here) and Optuna's GP has no constant liar, so with 8 workers it can propose near-duplicates: prefer `--workers 2-4` for Stage C.
- `front --name N [--select calmar|knee]` lists the feasible front and one pick (writes `front.json`). `stages plan --name sA` writes B1-B3 study files for the kept dimensions (signal / stops and sizing / regime and composition); a file with `from: <study>` gets its `fixed` values from that study's selected front trial at `study new`. `stages advise --name B3` applies the TDD's rule (hypervolume gain under 1% over 150 trials, importance on at most 25 parameters, no more than 30% categorical) and writes `C.yaml` (GP, 150 trials) when it says switch.
- Block 2 and 3 baselines are therefore "the earlier winner", not the live default; trial 0 of those blocks is that baseline.
- Not in this phase: the plateau-versus-spike selection test belongs to phase 4 (`robust.py`); here the known-optimum test is a smooth landscape with a constraint boundary. Regime-block "narrow bounds" are the register's own bounds.
