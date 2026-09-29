"""Shared helpers for the Ticker Data System stages (migrator, updator, archiver)."""

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

CONFIG_PATH = "app/config/market.json"
IST = timezone(timedelta(hours=5, minutes=30))  # India has no DST
COLS = ["Ticker", "Date", "Open", "High", "Low", "Close", "AdjClose", "Volume"]


class Busy(Exception):
    """Another stage holds the run lock."""


def load_config() -> dict:
    return json.loads(Path(os.environ.get("MARKET_CONFIG_PATH", CONFIG_PATH)).read_text(encoding="utf-8"))


def atomic(path: Path, write) -> None:
    """Write via write(tmp_path), then atomically rename over path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    write(tmp)
    os.replace(tmp, path)


def write_csv(df: pd.DataFrame, path: Path, **kw) -> None:
    atomic(path, lambda tmp: df.to_csv(tmp, index=False, **kw))


def iso(d: date) -> str:
    return d.isoformat()


def shift(day: str, days: int) -> str:
    return iso(date.fromisoformat(day) + timedelta(days=days))


@contextmanager
def run_lock(cfg: dict, log: logging.Logger):
    path = Path(cfg["paths"]["market"]) / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and time.time() - path.stat().st_mtime > cfg["lock"]["staleAfterHours"] * 3600:
        log.warning("Taking over stale lock %s", path)
        path.unlink(missing_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise Busy from None
    with os.fdopen(fd, "w") as f:
        f.write(f"{os.getpid()} {datetime.now(IST).isoformat(timespec='seconds')}")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def setup_logging(stage: str, cfg: dict, today: date) -> logging.Logger:
    log_file = Path(cfg["paths"]["logs"]) / f"market_{stage}_{today}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"market.{stage}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")):
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


@dataclass
class Report:
    """What one run did; becomes status.json, the reject file and the digest email."""

    stage: str
    rejects: list = field(default_factory=list)  # DataFrames with a Reason column
    failed: dict = field(default_factory=dict)  # ticker -> reason
    inactive: list = field(default_factory=list)
    rebuilt: dict = field(default_factory=dict)  # ticker -> reason
    updated: set = field(default_factory=set)
    notes: list = field(default_factory=list)
    registry_refreshed: bool | None = None
    last_trading_day: str | None = None
    error: str | None = None
    quiet: bool = False  # nothing to report (e.g. migration already done)

    def rejected(self) -> pd.DataFrame:
        return pd.concat(self.rejects, ignore_index=True) if self.rejects else pd.DataFrame(columns=COLS + ["Reason"])

    def status(self) -> str:
        return "failed" if self.error else "partial" if self.failed else "ok"

    def summary(self, now: datetime) -> dict:
        return {
            "status": self.status(),
            "stage": self.stage,
            "runDate": iso(now.date()),
            "finishedAt": now.isoformat(timespec="seconds"),
            "lastTradingDay": self.last_trading_day,
            "tickersUpdated": len(self.updated),
            "tickersFailed": sorted(self.failed),
            "tickersInactive": sorted(self.inactive),
            "rowsRejected": len(self.rejected()),
            "registryRefreshed": self.registry_refreshed,
        }

    def digest(self, now: datetime) -> tuple[str, str]:
        rej = self.rejected()
        lines = [f"Run date: {iso(now.date())}  Updated: {len(self.updated)}  Rejected rows: {len(rej)}"]
        if len(rej):
            lines.append("Rejections by rule: " + ", ".join(f"{k} {v}" for k, v in rej.Reason.value_counts().items()))
            lines.append("Top offending tickers: " + ", ".join(f"{k} ({v})" for k, v in rej.Ticker.value_counts().head(5).items()))
        for title, items in (
            ("Failed after retries", [f"{t}: {r}" for t, r in sorted(self.failed.items())]),
            ("Set inactive", sorted(self.inactive)),
            ("Split/dividend rebuilds", [f"{t}: {r}" for t, r in sorted(self.rebuilt.items())]),
            ("Notes", self.notes),
        ):
            if items:
                lines.append(f"{title}:\n  " + "\n  ".join(items))
        if self.error:
            lines.append(f"FATAL: {self.error}")
        return f"[Ticker Data] {self.stage} {self.status().upper()} {iso(now.date())}", "\n".join(lines)


def run_stage(stage: str, run) -> None:
    """Common entry point: lock, run, then reject file + status.json + digest. Exit 2 if busy, 1 on failure."""
    from app.market import mailer  # local import keeps smtplib out of the other modules

    cfg, now = load_config(), datetime.now(IST)
    log = setup_logging(stage, cfg, now.date())
    report = Report(stage)
    try:
        with run_lock(cfg, log):
            try:
                run(cfg, now, log, report)
            except Exception as e:
                log.exception("%s failed", stage)
                report.error = repr(e)
            if not report.quiet:
                rej = report.rejected()
                if len(rej):
                    write_csv(rej, Path(cfg["paths"]["logs"]) / "rejects" / f"{now.date()}_{stage}.csv")
                now = datetime.now(IST)
                atomic(
                    Path(cfg["paths"]["market"]) / "status.json",
                    lambda tmp: tmp.write_text(json.dumps(report.summary(now), indent=2), encoding="utf-8"),
                )
                mailer.send(cfg, *report.digest(now), log)
    except Busy:
        log.error("busy: another stage holds the run lock")
        sys.exit(2)
    if report.error:
        sys.exit(1)
