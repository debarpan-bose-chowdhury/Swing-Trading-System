# Swing-Trading-System

Batch-job pipeline for NSE data, signals and risk checks. Python 3.12, managed with `uv`. No database,
no server (except an optional stdlib HTTPS `/health`). Data is flat files under `app/data/` (git-ignored).
Full setup: `doc/LOCAL_SETUP.md`.

## Rules
- Run everything from the repo root (config/data paths are relative) with the venv active or via `uv run`.
- Stages are run as `python -m app.<package>.<stage>`. Use `--check` first (no network, no writes).
- Never read, print or commit `.env`. Never place orders: the broker client is read-only.
- Do not use `--force` without the user asking.

## Commands
- Tests: `uv run pytest` (offline; network is faked)
- Daily order: `app.metadata.data_source` -> `app.metadata.filter` -> `app.metadata.notifier` -> `app.market.updator` -> `app.risk.surveillance`
- Fridays: `app.analyst.signals` -> `app.risk.run` -> `app.risk.evaluate`
- One-time: `app.market.migrator` (slow, resumable). Quarterly: `app.market.archiver`, `app.metadata.cleaner`
- Optional: `app.analyst.ledger` (needs `ANGEL_*`), probes `app.analyst.probe --check-broker`, `app.risk.probe --check-nse`
- Replay: `app.analyst.signals --as-of YYYY-MM-DD` (read-only)

## Backtest (sibling `backtest/`, personal use, in progress)
- Design: `doc/Backtest_Engine_TDD.md`; history: `doc/Backtest_Implementation_Plan.md`. Check: `uv run --project backtest python -m backtest.run --check`. Tests: `uv run --project backtest pytest -c backtest/pyproject.toml`.
- Stages: `backtest.prep --check|--dividends|--scan`, `backtest.run --single|--compare [--set KEY=VALUE]` (--set applies params.json values, e.g. `--set sizing.minNewOrderInr=3000 --set sizing.minAdjustmentInr=1500`; at Rs 1 lakh the live sizing makes 0 trades) (`--no-auto-restart` keeps the live ladder: a flat-lock is never restarted; by default `ladder.autoRestart` in backtest.json simulates the owner's manual restart) (--compare [--workers N]: runs its 4 cases in parallel processes with a progress line per 5% and an ETA; today's names vs point-in-time, with 0/50/100% write-off of names that stop trading), `backtest.parity --check|--targets|--signals` (read-only parity of the backtest with the live app on your data: targets vs `app.analyst.signals --as-of` and stored live files; stored live signals vs the backtest's regime/rebalance rule/caps), `backtest.surv_proxy --snapshot|--calibrate`, `backtest.bench [--years N --profile --workers K]` (runtime spike), `backtest.bhav --check|--probe|--download|--build|--crosscheck|--summary|--universe-stats` (NSE bhavcopy; needs a passed `--probe` on a machine that can reach NSE). `backtest.universe --links|--probe-symbolchange|--validate-adjust|--tune-adjust|--build-pit` (point-in-time universe groundwork). Parameter bounds: `backtest/config/params.json` (unconfirmed until you set `confirmed`).
- Public API for an optimiser: `backtest/api.py` (only import from `backtest/` in `hpo/`). Every number is classified in `doc/parameter_register.csv` (`uv run --project backtest python -m backtest.register --write`; `test_register.py` fails on a new hardcoded number in the return-affecting modules: make it a config key with the old value as default). Default-parity fingerprint: `backtest.golden`.
- Reads `app/` and `app/data/` only; never writes them. Not copied into any Docker image.

## HPO (sibling `hpo/`, personal use, phases 1-3 built)
- Design: `doc/HPO_TDD.md`; notes and deviations: `doc/HPO_Implementation_Notes.md`. Check: `uv run --project hpo python -m hpo.cli --check`. Tests: `uv run --project hpo pytest -c hpo/pyproject.toml`.
- Commands: `hpo.cli space build|check|show`, `study new --config hpo/config/studies/s0.yaml` / `study run --name s0 [--workers 8]` / `study resume` / `study status`, `sensitivity --name`, `front --name [--select calmar|knee]`, `stages plan|advise --name`, `report --name`, `live --name`, `ledger show`; samplers sobol/tpe/gp (`gp` needs `--extra gp`). Writes only under `hpo/data/` (git-ignored); imports `app` and `backtest.api` only; not in any image.

## Exit codes
0 ok, 1 failed, 2 busy (run lock), 3 gate not met (run the upstream stage first).

## Outputs
`app/data/health.json`, `app/data/market/status.json`, `app/data/analyst/targets/targets_<date>.json`,
`app/data/risk/signals/signals_<asOf>.json`.

## Gotchas
- `analyst.json` ships with `"placeholders": true`; signals refuses to run until set false and capital is set.
- `risk.json` surveillance sources are placeholders until filled via `app.risk.probe --check-nse`; buys are blocked as `NO_SURVEILLANCE_DATA`.
- `cash_flows.csv` needs an `OPENING` row. Unset SMTP just skips email.
