"""Archiver: quarterly move of rows older than the cutoff from fresh CSV to archive Parquet."""

import logging
from datetime import datetime
from pathlib import Path

from app.market import registry
from app.market.common import Report, iso, run_stage, shift
from app.market.ingest import build_series


def run(cfg: dict, now: datetime, log: logging.Logger, report: Report) -> None:
    market = Path(cfg["paths"]["market"])
    cutoff = shift(iso(now.date()), -cfg["archiver"]["cutoffDays"])
    symbols = list(registry.load(market / "registry.csv").index)
    for s in build_series(cfg, symbols, cutoff):
        try:
            moved = s.store.archive_aged(s.key)
        except Exception as e:
            log.exception("Archive of %s failed", s.ticker)
            report.failed[s.ticker] = repr(e)
            continue
        if moved:
            report.updated.add(s.ticker)
            log.info("%s: archived %d rows older than %s", s.ticker, moved, cutoff)


if __name__ == "__main__":
    run_stage("archiver", run)
