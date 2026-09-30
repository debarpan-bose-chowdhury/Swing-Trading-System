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
REPLACE_ATTEMPTS = 6
COLS = ["Ticker", "Date", "Open", "High", "Low", "Close", "AdjClose", "Volume"]


class Busy(Exception):
    """Another stage holds the run lock."""


def safe_path(path: str | Path) -> Path:
    """Resolve path and require it to stay inside the working directory (/app in the container)."""
    base = os.path.realpath(os.getcwd())
    full = os.path.realpath(os.path.join(base, path))
    if os.path.commonpath([full, base]) != base:
        raise ValueError(f"path escapes the working directory: {path}")
    return Path(full)


def load_config() -> dict:
    cfg = json.loads(safe_path(CONFIG_PATH).read_text(encoding="utf-8"))
    cfg["paths"] = {k: str(safe_path(v)) for k, v in cfg["paths"].items()}
    return cfg


def atomic(path: Path, write) -> None:
    """Write via write(tmp_path), then atomically rename over path."""
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    write(tmp)
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows: a sync client (OneDrive), indexer or antivirus can briefly hold the destination open.
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.1 * 2**attempt)


def write_csv(df: pd.DataFrame, path: Path, **kw) -> None:
    atomic(path, lambda tmp: df.to_csv(tmp, index=False, **kw))


def iso(d: date) -> str:
    return d.isoformat()


def shift(day: str, days: int) -> str:
    return iso(date.fromisoformat(day) + timedelta(days=days))


@contextmanager
def run_lock(cfg: dict, log: logging.Logger):
    path = safe_path(Path(cfg["paths"]["market"]) / ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and time.time() - path.stat().st_mtime > cfg["lock"]["staleAfterHours"] * 3600:
        log.warning("Taking over a stale run lock")
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


def check_stage(stage: str) -> int:
    """`--check`: confirm the config and calendar load, with no network, lock, log file or data writes.

    Importing the stage module (which `python -m` already did) proves its dependencies are installed.
    """
    from app.market.tradingcal import Calendar  # local import: only needed here

    try:
        cfg = load_config()
        missing = [k for k in ("market", "logs", "calendar") if k not in cfg["paths"]]
        if missing:
            raise KeyError(f"paths missing from {CONFIG_PATH}: {', '.join(missing)}")
        Calendar(cfg["paths"]["calendar"])
    except Exception as e:
        print(f"{stage}: check FAILED: {e!r}", file=sys.stderr)
        return 1
    print(f"{stage}: check ok")
    return 0


def require_parquet_engine() -> None:
    """Fail fast, before any network fetch, when no parquet engine is installed."""
    try:
        import pyarrow  # noqa: F401
    except ImportError as e:
        raise RuntimeError("pyarrow is required for parquet storage; run `pip install -e .`") from e


def run_stage(stage: str, run, argv: list[str] | None = None) -> None:
    """Common entry point: lock, run, then reject file + status.json + digest. Exit 2 if busy, 1 on failure.

    With `--check` on the command line the stage only validates its environment (see check_stage).
    """
    if "--check" in (sys.argv[1:] if argv is None else argv):
        sys.exit(check_stage(stage))

    from app.market import mailer  # local import keeps smtplib out of the other modules

    cfg, now = load_config(), datetime.now(IST)
    log = setup_logging(stage, cfg, now.date())
    report = Report(stage)
    try:
        with run_lock(cfg, log):
            try:
                require_parquet_engine()
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
