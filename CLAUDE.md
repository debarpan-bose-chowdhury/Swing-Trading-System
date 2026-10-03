# Swing-Trading-System

Batch-job pipeline for NSE data, signals and risk checks. Python 3.12, managed with `uv`. No database,
no server (except an optional stdlib HTTPS `/health`). Data is flat files under `app/data/` (git-ignored).
Full setup: `app/doc/LOCAL_SETUP.md`.

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

## Exit codes
0 ok, 1 failed, 2 busy (run lock), 3 gate not met (run the upstream stage first).

## Outputs
`app/data/health.json`, `app/data/market/status.json`, `app/data/analyst/targets/targets_<date>.json`,
`app/data/risk/signals/signals_<asOf>.json`.

## Gotchas
- `analyst.json` ships with `"placeholders": true`; signals refuses to run until set false and capital is set.
- `risk.json` surveillance sources are placeholders until filled via `app.risk.probe --check-nse`; buys are blocked as `NO_SURVEILLANCE_DATA`.
- `cash_flows.csv` needs an `OPENING` row. Unset SMTP just skips email.
