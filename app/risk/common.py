"""Shared helpers for the Risk Manager stages: config, lock, status file, logging, run_stage, price history, state files."""

import argparse
import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from app.market.common import IST, Busy, atomic, iso, require_parquet_engine, safe_path, write_csv
from app.market.store import Store
from app.market.tradingcal import Calendar

CONFIG_PATH = "app/config/risk.json"
REGIMES = ("BULL", "TREND", "WEAK", "BEAR", "Unknown")
PLACEHOLDER = "<set after probe>"
SESSION_FINAL = "20:00"  # a session is final after this IST time (Ticker Data's sessionFinalAfterIST)
RETRY_MINUTES = 60
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
FLOW_TYPES = ("OPENING", "DEPOSIT", "WITHDRAWAL", "DIVIDEND", "OTHER")
TABLES = {  # state/*.csv layouts (all strings on disk): name -> (file, columns)
    "positions": ("positions.csv", ["ticker", "bucket", "track_start", "track_source", "last_seen"]),
    "cooldown": ("cooldown.csv", ["ticker", "trigger_date", "trigger_adj_close", "release_after"]),
    "deferred": ("deferred_drops.csv", ["ticker", "drop_date", "anniversary", "entry_date"]),
}


class Gate(Exception):
    """A run precondition is not met yet; the scheduler's hourly retry tries again (exit 3)."""


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, obj) -> None:
    atomic(path, lambda tmp: tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8"))


def is_date(v) -> bool:
    try:
        date.fromisoformat(str(v))
        return True
    except ValueError:
        return False


# --- config ------------------------------------------------------------------------------------------------------
def _num(v, lo: float = 0, hi: float | None = None, open_lo: bool = False) -> bool:
    ok = isinstance(v, (int, float)) and not isinstance(v, bool)
    return ok and (v > lo if open_lo else v >= lo) and (hi is None or v <= hi)


def _int(v, minimum: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= minimum


def load_config(stage: str = "run") -> dict:
    cfg = json.loads(safe_path(CONFIG_PATH).read_text(encoding="utf-8"))
    cfg["paths"] = {k: str(safe_path(v)) for k, v in cfg["paths"].items()}
    validate(cfg, stage)
    return cfg


def validate(cfg: dict, stage: str) -> None:
    """Config rules from the TDD ("Validation at load"). Raises ValueError naming the first violation.

    Adds cfg["buckets"] (cap buckets, highest first) and cfg["costs"] (the Analyst's cost model, read-only).
    """
    meta = json.loads(Path(cfg["paths"]["metadataConfig"]).read_text(encoding="utf-8"))
    analyst = json.loads(Path(cfg["paths"]["analystConfig"]).read_text(encoding="utf-8"))
    cfg["buckets"], cfg["costs"] = [b["name"] for b in meta["filter"]["capBuckets"]], analyst["costs"]
    buckets = cfg["buckets"]
    if stage == "run" and cfg["placeholders"]:
        raise ValueError("risk.json still has placeholders: true; replace the example values and set it to false")
    sz = cfg["sizing"]
    pcts = [sz["riskPerPositionPct"], sz["cashBufferPct"], sz["noTradeBand"]["relative"], sz["noTradeBand"]["absolutePct"], *sz["nameCapPct"].values()]
    if not all(_num(p, 0, 1, open_lo=True) for p in pcts) or not (_num(sz["minNewOrderInr"], 0, open_lo=True) and _num(sz["minAdjustmentInr"], 0, open_lo=True)):
        raise ValueError("sizing: percentages must be in (0, 1] and minNewOrderInr / minAdjustmentInr greater than 0")
    if not set(analyst["composition"]) <= set(sz["nameCapPct"]):
        raise ValueError("sizing.nameCapPct needs an entry for every bucket in the Analyst's composition")
    st = cfg["stops"]
    if not (_int(st["atrPeriod"], 2) and _num(st["atrMultiplier"], 0, open_lo=True) and st["bucketFallback"] in buckets):
        raise ValueError("stops: atrPeriod an integer of 2 or more, atrMultiplier > 0, bucketFallback a bucket name")
    if set(st["clampPct"]) != set(buckets) or not all(0 < lo < hi < 1 for lo, hi in st["clampPct"].values()):
        raise ValueError("stops.clampPct needs one [lo, hi] per bucket with 0 < lo < hi < 1")
    lv = cfg["ladder"]["levels"]
    draws, invested = [x["drawdownPct"] for x in lv], [x["maxInvestedPct"] for x in lv]
    if not (1 <= len(lv) <= 6 and draws == sorted(set(draws)) and invested == sorted(invested, reverse=True)
            and all(_num(d, 0, 1, open_lo=True) for d in draws) and all(_num(i, 0, 1) for i in invested) and all(i > 0 for i in invested[:-1])):
        raise ValueError("ladder.levels: 1 to 6 levels, drawdownPct ascending, maxInvestedPct descending (only the last may be 0)")
    rr, restart = cfg["ladder"]["reRisk"], cfg["ladder"]["restartFrom"]
    if not (set(rr["regimes"]) <= set(REGIMES) and _int(rr["consecutiveWeeks"], 1) and _int(rr["navAboveMinOfPreviousDays"], 1)):
        raise ValueError("ladder.reRisk: known regimes, consecutiveWeeks and navAboveMinOfPreviousDays integers of 1 or more")
    if restart is not None and not is_date(restart):
        raise ValueError("ladder.restartFrom must be null or an ISO date")
    caps = cfg["exposure"]["regimeCap"]
    if not set(caps) <= set(REGIMES) or not all(_num(v, 0, 1) for v in caps.values()):
        raise ValueError(f"exposure.regimeCap: only {REGIMES} with values in [0, 1]")
    liq = cfg["liquidity"]
    if not (_num(cfg["heat"]["capPct"], 0, 1, open_lo=True) and _int(liq["advDays"], 1) and set(liq["maxParticipationPct"]) >= set(buckets)
            and all(_num(v, 0, 1, open_lo=True) for v in liq["maxParticipationPct"].values())):
        raise ValueError("heat.capPct and liquidity.maxParticipationPct values must be in (0, 1], one per bucket; advDays >= 1")
    sv = cfg["surveillance"]
    if not (_int(sv["staleExitDays"], 0) and set(sv["exitOn"]) <= {"GSM", "T2T"} and _num(sv["blockEntryBandPct"])):
        raise ValueError("surveillance: staleExitDays integer >= 0, exitOn within GSM/T2T, blockEntryBandPct a number")
    if stage == "surveillance":
        for name in ("asm", "gsm", "t2t", "bands"):
            src = sv["sources"].get(name)
            if not isinstance(src, dict) or not str(src.get("url", "")).startswith("https://") or src.get("format") not in ("csv", "json") or not src.get("symbolColumn"):
                raise ValueError(f"surveillance.sources.{name} is not configured ({PLACEHOLDER}): run `python -m app.risk.probe --check-nse` and fill url, format, symbolColumn")
    tax = cfg["tax"]
    if not (all(_num(v) for k, v in tax["rates"].items() if k != "asOf") and _int(tax["deferral"]["windowDays"], 1)):
        raise ValueError("tax: rates must be numbers of 0 or more and deferral.windowDays an integer of 1 or more")
    g = cfg["gate"]
    m = re.fullmatch(r"(\w{3}) \d{2}:\d{2}", g["targetsWaitUntil"])
    if not (_num(g["maxMissingShare"], 0, 1) and _int(g["maxLedgerLagTradingDays"], 0) and m and m.group(1) in WEEKDAYS
            and re.fullmatch(r"\d{2}:\d{2}", g["retryUntil"])):
        raise ValueError("gate: maxMissingShare in [0, 1], maxLedgerLagTradingDays integer, targetsWaitUntil like 'Sun 22:00', retryUntil like '08:00'")
    ev = cfg["evaluator"]
    indices = json.loads(Path(cfg["paths"]["indices"]).read_text(encoding="utf-8"))["indices"]
    if ev["benchmark"] not in indices or not _num(ev["riskFreeRatePct"]):
        raise ValueError("evaluator.benchmark must be an index in indices.json and riskFreeRatePct a number of 0 or more")


# --- run lock, status, logging -----------------------------------------------------------------------------------
def risk_dir(cfg: dict) -> Path:
    return Path(cfg["paths"]["risk"])


@contextmanager
def run_lock(cfg: dict, log: logging.Logger):
    path = safe_path(risk_dir(cfg) / ".lock")
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
    log_file = Path(cfg["paths"]["logs"]) / f"risk_{stage}_{today}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"risk.{stage}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")):
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


@dataclass
class Report:
    """What one run did; its `block` becomes this stage's entry in risk_status.json and the digest email."""

    stage: str
    block: dict = field(default_factory=dict)
    lines: list = field(default_factory=list)  # digest lines
    keep: tuple = ()  # block keys carried over from the previous status when this run did not set them (lastGoodAsOf)
    partial: bool = False
    error: str | None = None
    quiet: bool = False  # nothing to persist or send

    def status(self) -> str:
        return "failed" if self.error else "partial" if self.partial else "ok"

    def digest(self, now: datetime) -> tuple[str, str]:
        lines = [f"Run date: {iso(now.date())}", *self.lines]
        if self.error:
            lines.append(f"FATAL: {self.error}")
        return f"[Risk] {self.stage} {self.status().upper()} {iso(now.date())}", "\n".join(lines)


def write_status(cfg: dict, report: Report, now: datetime) -> None:
    path = risk_dir(cfg) / "risk_status.json"
    data = read_json(path) or {}
    block = {"status": report.status(), "runDate": iso(now.date()), "finishedAt": now.isoformat(timespec="seconds"), **report.block}
    for k in report.keep:
        block.setdefault(k, data.get(report.stage, {}).get(k))
    data[report.stage] = block
    write_json(path, data)


def check_stage(stage: str) -> int:
    """`--check`: confirm config and calendar load; no network, lock, log file or data writes."""
    try:
        cfg = load_config(stage)
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
    parser = argparse.ArgumentParser(prog=f"app.risk.{stage}")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--force", action="store_true")
    if extra_args:
        extra_args(parser)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.check:
        sys.exit(check_stage(stage))

    from app.market import mailer  # local import keeps smtplib out of the other modules

    now = datetime.now(IST)
    try:
        cfg = load_config(stage)
    except Exception as e:  # invalid config: fail with a digest if the mail block is readable
        print(f"{stage}: config invalid: {e}", file=sys.stderr)
        raw = read_json(safe_path(CONFIG_PATH)) or {}
        if "mail" in raw:
            mailer.send(raw, f"[Risk] {stage} FAILED {now.date()}", f"Config invalid: {e}", logging.getLogger("risk"))
        sys.exit(1)
    log = setup_logging(stage, cfg, now.date())
    report = Report(stage)
    try:
        with run_lock(cfg, log):
            try:
                require_parquet_engine()
                run(cfg, now, log, report, args)
            except Gate as g:
                log.warning("gate not met: %s", g)
                sys.exit(3)
            except Exception as e:
                log.exception("%s failed", stage)
                report.error = str(e) or repr(e)
            if not report.quiet:
                now = datetime.now(IST)
                write_status(cfg, report, now)
                mailer.send(cfg, *report.digest(now), log)
    except Busy:
        log.error("busy: another stage holds the run lock")
        sys.exit(2)
    if report.error:
        sys.exit(1)


# --- dates and trading days --------------------------------------------------------------------------------------
def next_trading_day(cal: Calendar, day: date) -> date:
    day += timedelta(days=1)
    while not cal.is_trading_day(day):
        day += timedelta(days=1)
    return day


def add_trading_days(cal: Calendar, day: date, n: int) -> date:
    for _ in range(n):
        day = next_trading_day(cal, day)
    return day


def trading_days_between(cal: Calendar, a: str, b: str) -> int:
    """Trading days in (a, b]."""
    return len(cal.days(date.fromisoformat(a) + timedelta(days=1), date.fromisoformat(b))) if a < b else 0


def anniversary(d: date) -> date:
    """Same day one year later (29 Feb becomes 28 Feb)."""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return d.replace(year=d.year + 1, day=28)


def iso_week(d: str) -> str:
    y, w, _ = date.fromisoformat(d).isocalendar()
    return f"{y}-W{w:02d}"


# --- tables and price history ------------------------------------------------------------------------------------
def read_table(path: Path, cols: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=cols, dtype=str)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return df.reindex(columns=cols, fill_value="")


def history(store: Store, ticker: str, since: str, asof: str) -> pd.DataFrame:
    """Rows from both storage tiers up to asof (archive read only when the fresh file starts after `since`)."""
    df = store.read_fresh(ticker)
    if df.empty or df.Date.min() > since:
        df = pd.concat([store.read_archive(ticker), df], ignore_index=True)
    df = df[df.Date <= asof].drop_duplicates("Date", keep="last").sort_values("Date")
    return df.reset_index(drop=True)


@dataclass
class Context:
    """Everything one Run knows about the world at asOf (shared by the actual and the shadow portfolio)."""

    cfg: dict
    asof: str
    cal: Calendar
    store: Store
    log: logging.Logger
    buckets_of: dict  # bucket -> symbols of its newest file dated on or before asOf
    last_good: str | None = None
    rebalance: bool = False  # asOf is the last Monday-Friday trading day of its ISO week
    targets: dict | None = None  # the rebalance-day target file, when it applies
    windows: dict = field(default_factory=dict)  # bucket -> stock_trend_ma from the newest target file
    regime: dict = field(default_factory=lambda: {"raw": "Unknown", "active": "Unknown"})
    surv: dict | None = None
    warnings: list = field(default_factory=list)
    cache: dict = field(default_factory=dict)

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)

    def hist(self, ticker: str, since: str | None = None) -> pd.DataFrame:
        since = since or iso(date.fromisoformat(self.asof) - timedelta(days=120))
        got = self.cache.get(ticker)
        if got is None or got[0] > since:
            got = self.cache[ticker] = (since, history(self.store, ticker, since, self.asof))
        return got[1]

    def bucket(self, ticker: str, stored: str | None) -> str:
        """Newest bucket file listing the ticker, else the stored bucket, else the fallback (with a warning)."""
        for b in self.cfg["buckets"]:
            if ticker in self.buckets_of.get(b, ()):
                return b
        if stored:
            return stored
        self.warn(f"BUCKET_FALLBACK:{ticker}")
        return self.cfg["stops"]["bucketFallback"]


@dataclass
class Portfolio:
    """The actual book or the shadow book: where its state lives and what it holds."""

    name: str
    state: Path
    nav_file: Path
    signals: Path
    book: pd.DataFrame
    cash: float


def load_state(folder: Path) -> dict:
    st = {name: read_table(folder / file, cols) for name, (file, cols) in TABLES.items()}
    raw = folder / "ladder_state.json"
    if raw.exists():
        ladder = read_json(raw)
        if ladder is None:
            raise ValueError(f"{raw} is corrupt; restore it from backup/ or delete it and use ladder.restartFrom")
        st["ladder"] = ladder
    else:
        st["ladder"] = None
    return st


def save_state(folder: Path, st: dict) -> None:
    for name, (file, _) in TABLES.items():
        write_csv(st[name], folder / file)
    write_json(folder / "ladder_state.json", st["ladder"])
