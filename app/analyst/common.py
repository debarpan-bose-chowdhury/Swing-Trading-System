"""Shared helpers for the Stock Analyst stages (signals, and later ledger/probe)."""

import argparse
import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from app.analyst.regime import UNKNOWN_EXTRA
from app.market.common import IST, Busy, atomic, iso, require_parquet_engine, safe_path
from app.market.tradingcal import Calendar

CONFIG_PATH = "app/config/analyst.json"
REGIMES = ("BULL", "TREND", "WEAK", "BEAR")


SERIES_SUFFIX = re.compile(r"-[A-Z]{2}$")  # Angel One trading symbols carry the series, e.g. TATASTEEL-EQ


def tracked_symbol(tradingsymbol: str, known) -> str | None:
    """NSE symbol the Ticker Data registry knows for a broker trading symbol, None when it is not tracked."""
    if tradingsymbol in known:
        return tradingsymbol
    stripped = SERIES_SUFFIX.sub("", tradingsymbol)
    return stripped if stripped in known else None


class Gate(Exception):
    """A run precondition is not met yet; the scheduler's hourly retry tries again (exit 3)."""


def load_config() -> dict:
    cfg = json.loads(safe_path(CONFIG_PATH).read_text(encoding="utf-8"))
    cfg["paths"] = {k: str(safe_path(v)) for k, v in cfg["paths"].items()}
    validate(cfg)
    return cfg


def _int(v, minimum: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= minimum


def _num(v, minimum: float = 0) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= minimum


def validate(cfg: dict) -> None:
    """Config rules from the TDD ("Validation at load"). Raises ValueError naming the first violation."""
    buckets = {b["name"] for b in json.loads(Path(cfg["paths"]["metadataConfig"]).read_text(encoding="utf-8"))["filter"]["capBuckets"]}
    comp = cfg["composition"]
    if not set(comp) <= buckets:
        raise ValueError(f"composition buckets not in the Metadata config: {sorted(set(comp) - buckets)}")
    if abs(sum(comp.values()) - 1.0) > 0.001:
        raise ValueError("composition must sum to 1.0 (within 0.001)")
    for k, v in cfg["limits"].items():
        if not (_num(v) and 0 < v <= 1):
            raise ValueError(f"limits.{k} must be greater than 0 and at most 1")
    if not _int(cfg["regime"]["persistenceWeeks"], 1):
        raise ValueError("regime.persistenceWeeks must be an integer of 1 or more")
    if set(cfg["strategies"]) != set(REGIMES):
        raise ValueError(f"strategies must have exactly the regimes {REGIMES}")
    for regime, per_bucket in cfg["strategies"].items():
        for bucket in comp:
            s = per_bucket.get(bucket)
            if s is None:
                raise ValueError(f"strategies.{regime} is missing bucket {bucket}")
            if not (_int(s.get("top_n"), 0) and _int(s.get("lookback"), 1) and _int(s.get("stock_trend_ma"), 1)):
                raise ValueError(f"strategies.{regime}.{bucket}: top_n >= 0 and lookback, stock_trend_ma >= 1 must be integers")
    if cfg["rebalance"] != {"schedule": "weekly", "execution": "monday_open"}:
        raise ValueError("rebalance: only weekly and monday_open are supported")
    if not _num(cfg["capital"]["floatingCapitalInr"]):
        raise ValueError("capital.floatingCapitalInr must be a number of 0 or more")
    sel = cfg["selector"]
    if sel["maxStaleTradingDays"] != 0:
        raise ValueError("selector.maxStaleTradingDays: only 0 is supported (a ticker needs a row on the rebalance date)")
    if not _int(sel["momentumSkipDays"], 0):
        raise ValueError("selector.momentumSkipDays must be an integer of 0 or more")
    if not (_int(sel["liquidity"]["windowDays"], 1) and _num(sel["liquidity"]["minAdvCr"]) and sel["liquidity"]["statistic"] in ("median", "mean")):
        raise ValueError("selector.liquidity: windowDays >= 1, minAdvCr >= 0, statistic median or mean")
    bear = sel["bearScore"]
    weights = {k: v for k, v in bear.items() if k not in ("windows", "confirmThreshold")}
    if set(weights) != {"mom20", "mom63", "hit20", "vol20", "dd63"} or not all(_num(v, -1e9) for v in weights.values()):
        raise ValueError("selector.bearScore needs numeric weights for mom20, mom63, hit20, vol20, dd63")
    windows = bear.get("windows", {})
    if not (set(windows) <= {"shortDays", "longDays", "hitDays", "volDays", "ddDays"} and all(_int(v, 2) for v in windows.values())):
        raise ValueError("selector.bearScore.windows: only shortDays, longDays, hitDays, volDays, ddDays, integers of 2 or more")
    if not _num(bear.get("confirmThreshold", 0.0), -1):
        raise ValueError("selector.bearScore.confirmThreshold must be a number of -1 or more")
    if not (_num(sel.get("minMomentum", 0.0), -1) and _num(sel.get("trendBuffer", 0.0), -1)):
        raise ValueError("selector.minMomentum and selector.trendBuffer must be numbers of -1 or more")
    if not (_num(sel["maxMissingShare"]) and sel["maxMissingShare"] <= 1 and _int(sel["maxBucketFileAgeDays"], 0)):
        raise ValueError("selector: maxMissingShare must be 0..1 and maxBucketFileAgeDays an integer of 0 or more")
    if not _int(cfg["regime"]["minRows"], 1):
        raise ValueError("regime.minRows must be an integer of 1 or more")
    fast, slow, mom = (cfg["regime"].get(k, d) for k, d in (("smaFast", 50), ("smaSlow", 200), ("momentumDays", 63)))
    if not (_int(fast, 1) and _int(slow, 1) and _int(mom, 1) and fast < slow):
        raise ValueError("regime: smaFast < smaSlow and momentumDays must be integers of 1 or more")
    extra = cfg["regime"].get("unknownExtra", UNKNOWN_EXTRA)
    if not (_int(extra, 0) and _num(cfg["regime"].get("momentumThreshold", 0.0), -1)):
        raise ValueError("regime.unknownExtra must be an integer of 0 or more and regime.momentumThreshold a number of -1 or more")
    if cfg["regime"]["minRows"] < max(slow, mom) + extra + 1:
        raise ValueError("regime.minRows must be at least max(smaSlow, momentumDays) + unknownExtra + 1, or the regime is always Unknown")
    sig = cfg["signals"]
    if not (_int(sig["retryEveryMinutes"], 1) and _int(sig["maxHoldingsSnapshotAgeDays"], 0) and _int(sig["targetsRetentionWeeks"], 1)):
        raise ValueError("signals: retryEveryMinutes >= 1, maxHoldingsSnapshotAgeDays >= 0, targetsRetentionWeeks >= 1 must be integers")
    if not re.fullmatch(r"(?:\w{3} )?\d{2}:\d{2}", str(sig["retryUntil"])):
        raise ValueError("signals.retryUntil must look like 'Sun 22:00' or '22:00'")
    bad = [k for k, v in _leaves(cfg["costs"]) if not _num(v)]
    if bad:
        raise ValueError(f"costs must be numbers of 0 or more: {bad}")


def _leaves(d: dict, prefix: str = ""):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _leaves(v, f"{prefix}{k}.")
        elif k != "asOf":
            yield f"{prefix}{k}", v


@contextmanager
def run_lock(cfg: dict, log: logging.Logger):
    path = safe_path(Path(cfg["paths"]["analyst"]) / ".lock")
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
    log_file = Path(cfg["paths"]["logs"]) / f"analyst_{stage}_{today}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"analyst.{stage}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")):
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


@dataclass
class Report:
    """What one run did; its `block` becomes this stage's entry in analyst_status.json and the digest email."""

    stage: str
    block: dict = field(default_factory=dict)  # stage-specific status fields
    lines: list = field(default_factory=list)  # digest lines
    partial: bool = False
    error: str | None = None
    quiet: bool = False  # nothing to persist or send (read-only replay, nothing to do)

    def status(self) -> str:
        return "failed" if self.error else "partial" if self.partial else "ok"

    def digest(self, now: datetime) -> tuple[str, str]:
        lines = [f"Run date: {iso(now.date())}", *self.lines]
        if self.error:
            lines.append(f"FATAL: {self.error}")
        return f"[Analyst] {self.stage} {self.status().upper()} {iso(now.date())}", "\n".join(lines)


def write_status(cfg: dict, report: Report, now: datetime) -> None:
    """Merge this stage's block into analyst_status.json. lastGoodRunDate survives a later failed run."""
    path = Path(cfg["paths"]["analyst"]) / "analyst_status.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    good = iso(now.date()) if report.status() == "ok" else data.get(report.stage, {}).get("lastGoodRunDate")
    data[report.stage] = {
        "status": report.status(), "runDate": iso(now.date()), "finishedAt": now.isoformat(timespec="seconds"),
        **report.block, "lastGoodRunDate": good,
    }
    atomic(path, lambda tmp: tmp.write_text(json.dumps(data, indent=2), encoding="utf-8"))


def check_stage(stage: str) -> int:
    """`--check`: confirm config and calendar load; no network, lock, log file or data writes."""
    try:
        cfg = load_config()
        Calendar(cfg["paths"]["calendar"])
    except Exception as e:
        print(f"{stage}: check FAILED: {e!r}", file=sys.stderr)
        return 1
    print(f"{stage}: check ok")
    return 0


def run_stage(stage: str, run, argv: list[str] | None = None, extra_args=None) -> None:
    """Common entry point: lock, run, then status block + digest. Exit 2 busy, 3 gate not met, 1 failure.

    run(cfg, now, log, report, args). `extra_args(parser)` adds stage-specific flags.
    """
    parser = argparse.ArgumentParser(prog=f"app.analyst.{stage}")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--force", action="store_true")
    if extra_args:
        extra_args(parser)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.check:
        sys.exit(check_stage(stage))

    from app.market import mailer  # local import keeps smtplib out of the other modules

    cfg, now = load_config(), datetime.now(IST)
    log = setup_logging(stage, cfg, now.date())
    report = Report(stage)
    try:
        with run_lock(cfg, log):
            try:
                require_parquet_engine()  # price archives are Parquet; fail fast, before any work
                run(cfg, now, log, report, args)
            except Gate as g:
                log.warning("gate not met: %s", g)
                sys.exit(3)
            except Exception as e:
                log.exception("%s failed", stage)
                report.error = repr(e)
            if not report.quiet:
                now = datetime.now(IST)
                write_status(cfg, report, now)
                mailer.send(cfg, *report.digest(now), log)
    except Busy:
        log.error("busy: another stage holds the run lock")
        sys.exit(2)
    if report.error:
        sys.exit(1)
