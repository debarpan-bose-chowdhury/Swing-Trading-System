# HPO — implementation notes (phases 1, 1b, 2)

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
