"""Migrator: one-time bootstrap. Seed registry, download max history, split at the archive cutoff."""

import json
import logging
from datetime import datetime
from pathlib import Path

from app.market import calendar_sync, registry
from app.market.common import Report, atomic, iso, run_stage, shift
from app.market.fetcher import Fetcher
from app.market.ingest import backfill, build_series
from app.market.tradingcal import Calendar


def run(cfg: dict, now: datetime, log: logging.Logger, report: Report, fetcher: Fetcher | None = None) -> None:
    market = Path(cfg["paths"]["market"])
    if (market / "migration.done").exists():
        log.info("migration.done exists; nothing to do")
        report.quiet = True
        return
    today = iso(now.date())
    if not registry.upstream_ok(cfg, today):
        raise RuntimeError("upstream health.json is not healthy for today; nothing to seed from")

    reg = registry.load(market / "registry.csv")
    registry.refresh(reg, registry.upstream_symbols(cfg))
    registry.save(reg, market / "registry.csv")

    fetcher = fetcher or Fetcher(cfg, log)
    calendar_sync.sync(cfg, fetcher, log, report, now.date(), full=True)
    cal = Calendar(cfg["paths"]["calendar"])
    end = iso(cal.last_final_session(now, cfg["fetch"]["sessionFinalAfterIST"]))
    report.last_trading_day = end
    series = build_series(cfg, list(reg.index), shift(today, -cfg["archiver"]["cutoffDays"]))

    checkpoint = market / "migration_checkpoint.json"
    done = set(json.loads(checkpoint.read_text(encoding="utf-8"))) if checkpoint.exists() else set()

    def on_done(s) -> None:
        done.add(s.ticker)
        atomic(checkpoint, lambda tmp: tmp.write_text(json.dumps(sorted(done)), encoding="utf-8"))

    todo = [s for s in series if s.ticker not in done]
    log.info("Migrating %d of %d series (%d already checkpointed)", len(todo), len(series), len(done))
    backfill(fetcher, cal, cfg, report, todo, end, on_done)
    if all(s.ticker in done for s in series):
        atomic(market / "migration.done", lambda tmp: tmp.write_text(now.isoformat(timespec="seconds"), encoding="utf-8"))
        log.info("Migration complete")
    else:
        log.warning("Migration incomplete (%d failed); re-run to resume from the checkpoint", len(report.failed))


if __name__ == "__main__":
    run_stage("migrator", run)
