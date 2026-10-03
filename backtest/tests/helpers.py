"""Synthetic app/data tree in a temp working directory (config paths are relative to the cwd, like the app)."""

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from app.market.store import Store

CUTOFF = "2025-01-01"


def weekdays(start: str, n: int) -> list[str]:
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(1)
    return out


def bars(ticker: str, days: list[str], close=100.0, volume=1000, adj_factor=1.0) -> pd.DataFrame:
    c = pd.Series(close, index=range(len(days))) if not hasattr(close, "__len__") else pd.Series(close)
    f = pd.Series(adj_factor, index=range(len(days))) if not hasattr(adj_factor, "__len__") else pd.Series(adj_factor)
    return pd.DataFrame({"Ticker": ticker, "Date": days, "Open": c, "High": c + 1, "Low": c - 1, "Close": c, "AdjClose": c * f, "Volume": volume})


class TreeCase(unittest.TestCase):
    """Temp cwd with app/data/{market,storage}, app/config/nse_calendar.json and backtest/data."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.market, self.storage = Path("app/data/market"), Path("app/data/storage")
        self.storage.mkdir(parents=True)
        Path("app/config").mkdir(parents=True)
        Path("app/config/nse_calendar.json").write_text(json.dumps({"holidays": [], "specialSessions": []}))
        self.eq, self.idx = Store(self.market, CUTOFF), Store(self.market / "indices", CUTOFF)

    def bucket(self, name: str, symbols: list[str], day="2026-09-01") -> None:
        pd.DataFrame({"Symbol": symbols}).to_csv(self.storage / f"{name}_{day}.csv", index=False)

    def put(self, ticker: str, df: pd.DataFrame, index=False) -> None:
        (self.idx if index else self.eq).rebuild(ticker.lstrip("^"), df)
