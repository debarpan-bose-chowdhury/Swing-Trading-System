"""Shared fixtures for the market pipeline tests: fake Yahoo, config, data builders."""

import json
import logging
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from app.market import registry
from app.market.common import COLS, IST, Report
from app.market.fetcher import EXTRA, Fetcher
from app.market.store import Store

LOG = logging.getLogger("test.market")
NOW = datetime(2026, 9, 29, 21, 14, tzinfo=IST)  # Tuesday, after the 20:00 session cut-off
TODAY = "2026-09-29"
CUTOFF = "2025-09-29"


def weekdays(start: str, end: str) -> list[str]:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    return [(d0 + timedelta(n)).isoformat() for n in range((d1 - d0).days + 1) if (d0 + timedelta(n)).weekday() < 5]


def bars(ticker: str, days: list[str], close: float = 100.0, **cols) -> pd.DataFrame:
    df = pd.DataFrame(
        {"Ticker": ticker, "Date": days, "Open": close, "High": close + 1, "Low": close - 1, "Close": close,
         "AdjClose": close, "Volume": 1000, "Dividends": 0.0, "Splits": 0.0}
    )
    for k, v in cols.items():
        df[k] = v
    return df


class Yahoo:
    """Stand-in for Fetcher._yf. data: yahoo symbol -> long frame. script: per-call Exception / DataFrame / None."""

    def __init__(self, data: dict | None = None):
        self.data, self.script, self.calls = data or {}, [], []

    def __call__(self, fx, symbols, **kw):
        self.calls.append((list(symbols), kw))
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        if isinstance(step, pd.DataFrame):
            return step
        frames = [self.data[s] for s in symbols if s in self.data] or [pd.DataFrame(columns=COLS + EXTRA)]
        df = pd.concat(frames, ignore_index=True)
        if "start" in kw:
            df = df[(df.Date >= kw["start"]) & (df.Date < kw["end"])]
        return df.reset_index(drop=True)

    def symbols(self) -> list[str]:
        return [s for call, _ in self.calls for s in call]


def make_cfg(root: Path) -> dict:
    return {
        "paths": {
            "market": "market", "upstreamStorage": "storage", "upstreamHealth": "health.json",
            "logs": "logs", "calendar": "cal.json", "indices": "indices.json",
        },
        "fetch": {"batchSize": 2, "batchGapSeconds": 0, "hourlyRequestBudget": 2000, "blockPauseMinutes": 60,
                  "maxBlockPausesPerRun": 3, "maxRetries": 3, "backoffSeconds": [2, 4, 8], "nullRefetchPasses": 2,
                  "sessionFinalAfterIST": "20:00"},
        "validator": {"rejectShareRollback": 0.20},
        "updator": {"lookbackDays": 30, "deadTickerNoDataDays": 5, "dailyRunTime": "21:00"},
        "archiver": {"cutoffDays": 365, "cron": "0 22 2 1,4,7,10 *", "parquet": {"compression": "zstd"}},
        "lock": {"staleAfterHours": 6},
        "mail": {"smtpHost": "smtp.test", "smtpPort": 587, "sender": "a@test", "recipients": ["b@test"]},
    }


class Env(unittest.TestCase):
    """Temp dirs, calendar/indices files, no real sleeping, and a scriptable fake Yahoo."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        previous = os.getcwd()
        os.chdir(self.root)  # config paths are relative to the working directory, like /app in the container
        self.addCleanup(os.chdir, previous)
        self.cfg = make_cfg(self.root)
        self.market = Path(self.cfg["paths"]["market"])
        self.set_calendar()
        self.set_indices([])
        self.sleeps: list[float] = []
        patcher = patch("app.market.fetcher.time.sleep", side_effect=self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.yahoo = Yahoo()
        patcher = patch.object(Fetcher, "_yf", autospec=True, side_effect=self.yahoo)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.report = Report("test")
        self.store = Store(self.market, CUTOFF)
        self.idx_store = Store(self.market / "indices", CUTOFF)

    def set_calendar(self, holidays=(), special=()) -> None:
        Path(self.cfg["paths"]["calendar"]).write_text(json.dumps({"holidays": list(holidays), "specialSessions": list(special)}))

    def set_indices(self, indices: list[str]) -> None:
        Path(self.cfg["paths"]["indices"]).write_text(json.dumps({"indices": indices}))

    def write_upstream(self, symbols: list[str], day: str = TODAY, healthy: bool = True, checked: str | None = None) -> None:
        storage = Path(self.cfg["paths"]["upstreamStorage"])
        storage.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"Symbol": symbols, "MarketCap": 1, "InceptionDate": "2000-01-01"}).to_csv(storage / f"LargeCap_{day}.csv", index=False)
        Path(self.cfg["paths"]["upstreamHealth"]).write_text(json.dumps(
            {"status": "healthy" if healthy else "unhealthy", "checkedAt": checked or f"{TODAY}T19:45:00+05:30", "missing": []}))

    def set_registry(self, symbols: list[str], **overrides) -> None:
        reg = registry.load(self.market / "registry.csv")
        registry.refresh(reg, {s: "2026-09-01" for s in symbols})
        for col, val in overrides.items():
            reg[col] = val
        registry.save(reg, self.market / "registry.csv")

    def registry(self) -> pd.DataFrame:
        return registry.load(self.market / "registry.csv")

    def stored(self, ticker: str, days: list[str], close: float = 100.0, store: Store | None = None, **cols) -> None:
        (store or self.store).upsert(ticker.lstrip("^"), bars(ticker, days, close, **cols))

    def yahoo_has(self, symbol: str, days: list[str], close: float = 100.0, **cols) -> pd.DataFrame:
        self.yahoo.data[symbol] = bars(symbol, days, close, **cols)
        return self.yahoo.data[symbol]

    def fetcher(self) -> Fetcher:
        return Fetcher(self.cfg, LOG)
