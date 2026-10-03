# Running Swing-Trading-System locally (no Docker)

This system is a set of batch jobs (`python -m app.<package>.<stage>`) that read and write flat files
under `app/data/`. There is no database or queue. The only long-running process is an optional HTTPS
health endpoint used by CI. Everything below runs from the **repository root**, because config and data
paths are relative.

## 1. Prerequisites

| Tool | Why | Check |
|---|---|---|
| Python 3.12 | `requires-python >= 3.12` (the system `python3` may be older) | `python3.12 --version` |
| [uv](https://docs.astral.sh/uv/) | installs from `uv.lock` | `uv --version` |
| git | clone | `git --version` |
| openssl | only for the optional health service | `openssl version` |
| Node 18+ and Claude Code | to drive the system from the CLI | `claude --version` |

Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh`
Install Claude Code: `npm install -g @anthropic-ai/claude-code`, then run `claude` once and log in.

You also need outbound internet access (NSE and Yahoo Finance). No Anthropic or market-data API key is needed by the pipeline.

## 2. Get the code

```bash
git clone https://github.com/debarpan-bose-chowdhury/Swing-Trading-System.git
cd Swing-Trading-System
```

## 3. Install dependencies

```bash
uv sync --python 3.12              # creates .venv from uv.lock
uv pip install pytest pytest-cov   # test tools (not in the lock file)
source .venv/bin/activate          # or prefix every command below with `uv run`
```

## 4. Verify the install

```bash
pytest                              # offline: Yahoo, NSE and the broker are faked
for m in app.metadata.data_source app.metadata.filter app.metadata.notifier app.metadata.cleaner \
         app.market.migrator app.market.updator app.market.archiver \
         app.analyst.signals app.analyst.ledger \
         app.risk.run app.risk.surveillance app.risk.evaluate; do
  python -m $m --check || echo "FAILED: $m"
done
```

`--check` validates config and imports only: no network, no writes. On a fresh clone `app.risk.surveillance --check` exits 1 until step 6.2 is done; every other stage exits 0 (verified; 635 tests pass).

## 5. Secrets (optional)

Only needed for the Angel One ledger/probe (`ANGEL_*`) and for digest emails (`SMTP_*`).

```bash
cp .env.example .env     # .env is git-ignored
# edit .env: NAME=value, no quotes, one per line
set -a; source .env; set +a   # the code reads os.environ only; there is no dotenv loader
```

A value that is blank or still looks like `<set at deployment>` counts as unset. Unset SMTP means emails are skipped with a warning; runs do not fail.

## 6. Configure (`app/config/`)

1. `analyst.json`: set `"placeholders": false` and `capital.floatingCapitalInr` (ships as 0). Signals refuses to run while placeholders is `true`.
2. `risk.json`: surveillance sources are `<set after probe>`. Run `python -m app.risk.probe --check-nse` and fill them in. Until then Surveillance exits 1 and every buy is blocked as `NO_SURVEILLANCE_DATA` (exits still work).
3. `cash_flows.csv`: header-only; add an `OPENING` row. Header: `date,type,amount_inr,note`.
4. `seed_positions.csv`: add existing holdings if any.
5. `mail.*` in `market.json`, `analyst.json`, `risk.json`: optional.

## 7. Run the pipeline

Run in this order (the original schedule is IST):

```bash
# Metadata: daily
python -m app.metadata.data_source      # writes app/data/raw/<date>.csv
python -m app.metadata.filter           # writes app/data/storage/
python -m app.metadata.notifier         # writes app/data/health.json

# Ticker data
python -m app.market.migrator           # ONE-TIME, slow (15 s throttle), resumable; needs today's healthy health.json
python -m app.market.updator            # daily
# python -m app.market.archiver         # quarterly
# python -m app.metadata.cleaner        # quarterly

# Risk / analyst
python -m app.risk.surveillance         # daily, after the probe/config step
python -m app.analyst.signals           # Fridays
python -m app.risk.run
python -m app.risk.evaluate
python -m app.analyst.ledger            # optional, needs ANGEL_* (read-only broker client)
```

Flags: `--check` (validate only), `--force` (supersede existing output), `--as-of YYYY-MM-DD` (read-only replay for signals).
Exit codes: `0` ok, `1` failed, `2` busy (run lock held), `3` gate not met (run the upstream stage first).

Outputs to inspect: `app/data/health.json`, `app/data/market/status.json`,
`app/data/analyst/targets/targets_<date>.json`, `app/data/risk/signals/signals_<asOf>.json`.

## 8. Optional: health endpoint

```bash
mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -subj /CN=localhost -keyout certs/key.pem -out certs/cert.pem
TLS_CERTFILE=certs/cert.pem TLS_KEYFILE=certs/key.pem PORT=8080 python -m app
curl -k https://localhost:8080/health      # {"status":"ok"}
```

## 9. Operate it with the Claude Code CLI

```bash
cd Swing-Trading-System
source .venv/bin/activate
set -a; source .env; set +a      # only if you use Angel One / SMTP
claude
```

Claude reads `CLAUDE.md` (commands, order, gotchas) and `.claude/settings.json` (pre-approved commands) from the repo. Example prompts:

- "Run the daily metadata pipeline and summarise `app/data/health.json`."
- "Run `app.market.updator --check`, then run it and report `status.json`."
- "Run analyst signals with `--as-of 2026-09-25` and explain the targets."
- "Run risk surveillance and tell me why any buys are blocked."
- "Run the tests and fix any failures."

Non-interactive: `claude -p "run the metadata pipeline and report health.json"`.
Keep secrets in `.env`; never paste them into prompts. Claude is configured not to read `.env`.

## 10. Optional: backtest

The backtest is a separate uv project (`backtest/`) that reads `app/data/` and writes only `backtest/data/`. It needs the pipeline's price history (section 7) first.

```
uv run --project backtest python -m backtest.run --check
uv run --project backtest python -m backtest.prep --check
uv run --project backtest python -m backtest.run --single
uv run --project backtest pytest -c backtest/pyproject.toml    # offline tests
```

The point-in-time universe needs NSE bhavcopy downloads (`backtest.bhav --probe` then `--download`, slow and resumable). Full guide: `doc/Backtest_Engine_TDD.md`.

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| `requires-python` / syntax errors | use Python 3.12: `uv sync --python 3.12` |
| File-not-found for config/data | run from the repo root |
| Exit code 3 | an upstream stage has not produced today's output (e.g. health.json unhealthy) |
| Exit code 2 | another run holds the lock; wait or check for a stale `.lock` in `app/data/market/` |
| Signals refuses to start | `"placeholders": true` in `analyst.json` |
| `NO_SURVEILLANCE_DATA` | fill `risk.json` sources via `app.risk.probe --check-nse` |
| NSE/Yahoo errors | outbound access or rate limits; retry later |
| Ledger/probe fail | missing `ANGEL_*` env vars |
